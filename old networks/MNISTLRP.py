# imports
from __future__ import print_function
import argparse
import os
import sys
import random
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as datasets
import torchvision.transforms as transforms
import torchvision.utils as vutils
import modules.ModuleRedefinitions as nnrd
import models._DCGAN as dcgm
from utils.utils import Logger
import subprocess
import numpy as np

# add parameters
parser = argparse.ArgumentParser()
parser.add_argument('--batchSize', type=int, default=64, help='input batch size')
parser.add_argument('--dataset', help='mnist', default='mnist')
parser.add_argument('--nz', type=int, default=100, help='size of the latent z vector')
parser.add_argument('--ngf', type=int, default=64, help='number of generator filters in first layer')
parser.add_argument('--ndf', type=int, default=64, help='number of discriminator filters in first layer')
parser.add_argument('--epochs', type=int, default=25, help='number of epochs to train for')
parser.add_argument('--outf', default='output', help='folder to output images and model checkpoints')
parser.add_argument('--ngpu', type=int, default=1, help='number of GPUs to use')
parser.add_argument('--imageSize', type=int, default=64)
parser.add_argument('--loadG', default='', help='path to generator (to continue training')
parser.add_argument('--loadD', default='', help='path to discriminator (to continue training')
parser.add_argument('--alpha', default=1, type=float)
parser.add_argument('--beta', default=None, type=float)
parser.add_argument('--lflip', help='Flip the labels during training', action='store_true')
parser.add_argument('--nolabel', help='Print the images without labeling of probabilities', action='store_true')
parser.add_argument('--freezeG', help='Freezes training for G after epochs / 3 epochs', action='store_true')
parser.add_argument('--freezeD', help='Freezes training for D after epochs / 3 epochs', action='store_true')

opt = parser.parse_args()
outf = '{}/{}'.format(opt.outf, os.path.splitext(os.path.basename(sys.argv[0]))[0])
checkpointdir = '{}/{}'.format(outf, 'checkpoints')
ngpu = int(opt.ngpu)
ngf = int(opt.ngf)
ndf = int(opt.ndf)
nz = int(opt.nz)
alpha = opt.alpha
beta = opt.beta
p = 2
print(opt)

try:
    os.makedirs(outf)
except OSError:
    pass

try:
    os.makedirs(checkpointdir)
except OSError:
    pass

# CUDA everything
cudnn.benchmark = True
gpu = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
torch.set_default_dtype(torch.float32)
if torch.cuda.is_available():
    torch.set_default_tensor_type('torch.cuda.FloatTensor')
else:
    torch.set_default_tensor_type('torch.FloatTensor')
print(gpu)

# load datasets
if opt.dataset == 'mnist':
    out_dir = 'dataset/MNIST'
    dataset = datasets.MNIST(root=out_dir, train=True, download=True,
                             transform=transforms.Compose(
                                 [
                                     transforms.Resize(opt.imageSize),
                                     transforms.ToTensor(),
                                     transforms.Normalize((0.5,), (0.5,)),
                                 ]
                             ))
    nc = 1
else:
    pass
assert dataset
idx = dataset.train_labels != 8
dataset.train_labels[idx] = 0
idx = dataset.train_labels == 8
dataset.train_labels[idx] = 1

dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batchSize,
                                         shuffle=True, num_workers=2)


# misc. helper functions

def discriminator_target(size):
    """
    Tensor containing soft labels, with shape = size
    """
    # noinspection PyUnresolvedReferences
    if not opt.lflip:
        return torch.Tensor(size).uniform_(0.7, 1.0)
    return torch.Tensor(size).zero_()


def generator_target(size):
    """
    Tensor containing zeroes, with shape = size
    :param size: shape of vector
    :return: zeros tensor
    """
    # noinspection PyUnresolvedReferences
    if not opt.lflip:
        return torch.Tensor(size).zero_()
    return torch.Tensor(size).uniform_(0.7, 1.0)


# init networks

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


# discriminator = DiscriminatorNet(ngpu).to(gpu)
discriminator = dcgm.DiscriminatorNetLessCheckerboard(nc, ndf, alpha, beta, ngpu).to(gpu)
discriminator.apply(weights_init)
if opt.loadD != '':
    discriminator.load_state_dict(torch.load(opt.loadG))

# init optimizer + loss

d_optimizer = optim.Adam(discriminator.parameters(), lr=0.0002, betas=(0.5, 0.999))

loss = nn.BCELoss()

# init fixed noise

# Create Logger instance
logger = Logger(model_name='LRPGAN', data_name=opt.dataset, dir_name=outf)
print('Created Logger')
# training

for epoch in range(opt.epochs):
    for n_batch, (batch_data, label) in enumerate(dataloader, 0):
        batch_size = batch_data.size(0)

        ############################
        # Train Discriminator
        ###########################
        # train with real
        discriminator.zero_grad()
        real_data = batch_data.to(gpu)
        real_data = F.pad(real_data, (p, p, p, p), value=-1)
        label = label.to(gpu)
        # save input without noise for relevance comparison
        # Add noise to input
        label = label.float()
        prediction_real = discriminator(real_data)
        d_err_real = loss(prediction_real, label)
        d_err_real.backward()
        d_real = prediction_real.mean().item()

        if n_batch % 10 == 0:
            print('[%d/%d][%d/%d] Loss_D: %.4f Loss_G: %.4f D(x): %.4f D(G(z)): %.4f / %.4f'
                  % (epoch, opt.epochs, n_batch, len(dataloader),
                     d_real, d_real, d_real, d_real, d_real))

        if n_batch % 100 == 0:

            idx = label.nonzero()
            if len(idx) == 0:
                idx = 0
            else:
                idx = idx[0].item()

            test_fake = F.pad(batch_data[idx], (p, p, p, p), value=-1).unsqueeze(0).to(gpu)
            test_fake.requires_grad = True

            # set ngpu to one, so relevance propagation works
            if (opt.ngpu > 1):
                discriminator.setngpu(1)

            # eval needs to be set so batch norm works with batch size of 1
            test_result = discriminator(test_fake)
            test_relevance = discriminator.relprop()

            idx = (label == 0).nonzero()
            if len(idx) == 0:
                idx = 0
            else:
                idx = idx[0].item()

            # Relevance propagation on real image
            real_test = F.pad(batch_data[idx], (p, p, p, p), value=-1).unsqueeze(0).to(gpu)
            real_test.requires_grad = True
            real_test_result = discriminator(real_test)
            real_test_relevance = discriminator.relprop()

            # set ngpu back to opt.ngpu
            if (opt.ngpu > 1):
                discriminator.setngpu(opt.ngpu)

            # Add up relevance of all color channels
            test_relevance = torch.sum(test_relevance, 1, keepdim=True)
            real_test_relevance = torch.sum(real_test_relevance, 1, keepdim=True)

            test_fake = torch.cat((test_fake[:, :, p:-p, p:-p], real_test[:, :, p:-p, p:-p]))
            test_relevance = torch.cat((test_relevance[:, :, p:-p, p:-p], real_test_relevance[:, :, p:-p, p:-p]))
            printdata = {'test_result': test_result.item(), 'real_test_result': real_test_result.item(),
                         'min_test_rel': torch.min(test_relevance), 'max_test_rel': torch.max(test_relevance),
                         'min_real_rel': torch.min(real_test_relevance), 'max_real_rel': torch.max(real_test_relevance)}

            img_name = logger.log_images(
                test_fake.detach(), test_relevance.detach(), test_fake.size(0),
                epoch, n_batch, len(dataloader), printdata, noLabel=opt.nolabel
            )

            # show images inline
            comment = '{:.4f}-{:.4f}'.format(printdata['test_result'], printdata['real_test_result'])

            subprocess.call([os.path.expanduser('~/.iterm2/imgcat'),
                             outf + '/mnist/epoch_' + str(epoch) + '_batch_' + str(n_batch) + '_' + comment + '.png'])

            # status = logger.display_status(epoch, opt.epochs, n_batch, len(dataloader), d_error_total, d_error_total,
            #                                prediction_real, prediction_fake)

    # do checkpointing
    torch.save(discriminator.state_dict(), '%s/generator.pth' % (checkpointdir))
