# imports
from __future__ import print_function
import argparse
import os
import shutil
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
import torch.distributions as distr
import models._DRAGAN as dcgm
import utils.utils as utils
from utils.utils import Logger
from utils.utils import MidpointNormalize
import subprocess
import errno
import matplotlib.pyplot as plt
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
parser.add_argument('--alpha', default=1, type=int)
parser.add_argument('--lflip', help='Flip the labels during training', action='store_true')
parser.add_argument('--nolabel', help='Print the images without labeling of probabilities', action='store_true')
parser.add_argument('--freezeG', help='Freezes training for G after epochs / 3 epochs', action='store_true')
parser.add_argument('--freezeD', help='Freezes training for D after epochs / 3 epochs', action='store_true')
parser.add_argument('--fepochs', help='Number of epochs before freeze', type=int, default=None)
parser.add_argument('--lr_g', help='Learning rate for optimizer, 0.00005 for lrp?', type=float, default=0.0002)
parser.add_argument('--lr_d', help='Learning rate for optimizer, 0.00005 for lrp?', type=float, default=0.0002)
parser.add_argument('--eps_init', help='Change epsilon for eps rule after loading state dict', type=float, default=None)
parser.add_argument('--d_lambda', help='Factor for gradient penalty, default=10', type=float, default=10)
parser.add_argument('--cuda', help='number of GPU', type=int, default=0)
parser.add_argument('--gp', help='Use gradient penalty', action='store_true')
parser.add_argument('--cont', help='Continue training -> Does not delete dir', default=None, type=int)
parser.add_argument('--split', help='Split dataset in training and test set', action='store_true')
parser.add_argument('--comment', help='Comment to add to run parameter file', default='', required=True)
parser.add_argument('--add_noise', help='Use additive noise to stabilize trainint', action='store_true')
parser.add_argument('--imgcat', action='store_true')

opt = parser.parse_args()
outf = '{}/{}/{}_{}'.format(opt.outf, os.path.splitext(os.path.basename(sys.argv[0]))[0], opt.dataset, opt.comment)
checkpointdir = '{}/{}'.format(outf, 'checkpoints')
ngpu = int(opt.ngpu)
ngf = int(opt.ngf)
ndf = int(opt.ndf)
nz = int(opt.nz)
alpha = opt.alpha
p = 1
lambda_ = float(opt.d_lambda)

print(opt)

if opt.fepochs:
    freezeEpochs = int(opt.fepochs)

else:
    freezeEpochs = opt.epochs // 3

if not opt.cont:
    try:
        shutil.rmtree(outf)
    except OSError:
        pass
try:
    os.makedirs(outf)
except OSError as e:
    if e.errno != errno.EEXIST:
        raise

try:
    os.makedirs(checkpointdir)
except OSError:
    pass

text_file = open("{}/run_parameters.txt".format(outf), "w+")
text_file.write("Run parameters: %s" % opt)
text_file.close()

# CUDA everything
cudnn.benchmark = True
gpu = torch.device('cuda:{}'.format(opt.cuda) if torch.cuda.is_available() else 'cpu')
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

elif opt.dataset == 'anime':
    root_dir = 'dataset/faces'
    dataset = datasets.ImageFolder(root=root_dir, transform=transforms.Compose(
        [
            transforms.Resize((opt.imageSize, opt.imageSize)),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        ]
    ))
    nc = 3

elif opt.dataset == 'portrait':
    root_dir = 'dataset/portrait'
    dataset = datasets.ImageFolder(root=root_dir, transform=transforms.Compose(
        [
            transforms.Resize((opt.imageSize, opt.imageSize)),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        ]
    ))
    nc = 3

else:
    pass

assert dataset

if opt.split:
    idx_train = np.arange(0, int(len(dataset) * 0.8) + 1, 1)
    idx_test = np.arange(int(len(dataset) * 0.8) + 1, len(dataset), 1)
    trainingset = torch.utils.data.dataset.Subset(dataset, idx_train)
    test_set = torch.utils.data.dataset.Subset(dataset, idx_test)
    dataset = trainingset

dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batchSize,
                                         shuffle=True, num_workers=2)


# misc. helper functions

def draw_chisquare(size):
    chi_distr = distr.Chi2(1.0)
    samples = chi_distr.sample((size,))
    return samples


def added_gaussian_chi(ins, stddev):
    if stddev > 0:
        noise = torch.Tensor(torch.randn(ins.size()).to(gpu) * stddev)
        chi = draw_chisquare(ins.size(0)).to(gpu)
        chi_scaled_noise = noise * chi.reshape(-1, 1, 1, 1)
        return ins + chi_scaled_noise
    return ins


def added_gaussian(ins, stddev):
    if stddev > 0:
        noise = torch.Tensor(torch.randn(ins.size()).to(gpu) * stddev)
        return ins + noise
    return ins


def adjust_variance(variance, initial_variance, num_updates):
    return max(variance - initial_variance / num_updates, 0)


def soft_real_label(size):
    """
    Tensor containing soft labels, with shape = size
    """
    # noinspection PyUnresolvedReferences
    if not opt.lflip:
        return torch.Tensor(size).uniform_(0.8, 1.0)
    return torch.Tensor(size).zero_()


def soft_fake_label(size):
    """
    Tensor containing zeroes, with shape = size
    :param size: shape of vector
    :return: zeros tensor
    """
    # noinspection PyUnresolvedReferences
    if not opt.lflip:
        return torch.Tensor(size).zero_()
    return torch.Tensor(size).uniform_(0.8, 1.0)


def real_label(size):
    """
    :param size:
    :return:
    """
    return torch.ones(size)


# init networks

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


# generator = GeneratorNet(ngpu).to(gpu)
ref_noise = torch.randn(1, nz, 1, 1, device=gpu)
generator = dcgm.GeneratorNetLessCheckerboardUpsample(nc, ngf, ngpu).to(gpu)
generator.apply(weights_init)
if opt.loadG != '':
    dict = torch.load(opt.loadG, map_location='cuda:0' if torch.cuda.is_available() else 'cpu')
    if torch.__version__ == '0.4.0':
        del dict['net.1.num_batches_tracked']
        del dict['net.4.num_batches_tracked']
        del dict['net.7.num_batches_tracked']
        del dict['net.10.num_batches_tracked']
        del dict['net.13.num_batches_tracked']
    generator.load_state_dict(dict)
    generator.to(gpu)

discriminator = dcgm.DiscriminatorNetLessCheckerboardToCanonical(nc, ndf, alpha, ngpu).to(gpu)
discriminator.apply(weights_init)
if opt.loadD != '':
    dict = torch.load(opt.loadD, map_location='cuda:0' if torch.cuda.is_available() else 'cpu')
    if torch.__version__ == '0.4.0':
        del dict['net.1.bn2.num_batches_tracked']
        del dict['net.2.bn3.num_batches_tracked']
        del dict['net.3.bn4.num_batches_tracked']
        del dict['net.4.bn5.num_batches_tracked']
    discriminator.load_state_dict(dict)
    discriminator.to(gpu)

# Set all weights in smoothing layer to 1
# discriminator.net[0][0].weight.fill_(1)


if opt.eps_init:
    def eps_init(m):
        classname = m.__class__.__name__
        if classname.find('Eps') != -1:
            m.epsilon = float(opt.eps_init)


    discriminator.apply(eps_init)

# init optimizer + loss

d_optimizer = optim.Adam(discriminator.parameters(), lr=float(opt.lr_d), betas=(0.5, 0.999))
g_optimizer = optim.Adam(generator.parameters(), lr=float(opt.lr_g), betas=(0.5, 0.999))

loss = nn.BCELoss()

# init fixed noise

fixed_noise = torch.randn(1, nz, 1, 1, device=gpu)

# Additive noise to stabilize Training for DCGAN
initial_additive_noise_var = 0.1
add_noise_var = 0.1

# Create Logger instance
logger = Logger(model_name='LRPGAN', data_name=opt.dataset, dir_name=outf, make_fresh=True if not opt.cont else False)
print('Created Logger')
# training

for epoch in range(opt.epochs):
    for n_batch, (batch_data, _) in enumerate(dataloader, 0):
        batch_size = batch_data.size(0)
        add_noise_var = adjust_variance(add_noise_var, initial_additive_noise_var, opt.epochs * len(dataloader) * 1 / 4)

        ############################
        # Train Discriminator
        ###########################
        # train with real

        discriminator.zero_grad()
        real_data = batch_data.to(gpu)
        real_test = real_data[0].clone().unsqueeze(0)

        real_data = F.pad(real_data, (p, p, p, p), mode='replicate')
        label_real = soft_real_label(batch_size).to(gpu)

        # save input without noise for relevance comparison

        # Add noise to input
        if opt.add_noise:
            real_data = added_gaussian_chi(real_data, add_noise_var)

        prediction_real = discriminator(real_data)
        d_err_real = loss(prediction_real, label_real)
        d_err_real.backward()
        d_real = prediction_real.mean().item()

        # train with fake
        noise = torch.randn(batch_size, nz, 1, 1, device=gpu)
        fake = generator(noise)
        fake = F.pad(fake, (p, p, p, p), mode='replicate')
        label_fake = soft_fake_label(batch_size).to(gpu)

        # Add noise to fake
        if opt.add_noise:
            fake = added_gaussian_chi(fake, add_noise_var)

        prediction_fake = discriminator(fake.detach())
        d_err_fake = loss(prediction_fake, label_fake)
        d_err_fake.backward()
        d_fake_1 = prediction_fake.mean().item()

        d_error_total = d_err_real.item() + d_err_fake.item()

        # gradient penalty

        if opt.gp:
            grad_alpha = torch.rand(batch_size, nc, 1, 1).expand(real_data.size())
            x_gp = torch.Tensor(grad_alpha * real_data.data + (1 - grad_alpha) * (real_data.data + 0.5 * real_data.data.std() * torch.rand(real_data.size())),
                                requires_grad=True)
            pred_hat = discriminator(x_gp)
            gradients = torch.autograd.grad(outputs=pred_hat, inputs=x_gp, grad_outputs=torch.ones(pred_hat.size()),
                                            create_graph=True, retain_graph=True, only_inputs=True)[0]
            gradient_penalty = lambda_ * ((gradients.norm(2, dim=1) - 1) ** 2).mean()
            gradient_penalty.backward()
            d_error_total += gradient_penalty.item()

        # only update uf we don't freeze discriminatorx
        if not opt.freezeD or (opt.freezeD and epoch <= freezeEpochs):
            d_optimizer.step()

        ############################
        # Train Generator
        ###########################
        generator.zero_grad()
        noise = torch.randn(batch_size, nz, 1, 1, device=gpu)
        fake = generator(noise)
        fake = F.pad(fake, (p, p, p, p), mode='replicate')

        # Add noise to fake
        if opt.add_noise:
            fake = added_gaussian_chi(fake, add_noise_var)

        prediction_fake_g = discriminator(fake)
        label_real = real_label(batch_size).to(gpu)
        g_err = loss(prediction_fake_g, label_real)
        g_err.backward()
        d_fake_2 = prediction_fake_g.mean().item()

        # only update if we don't freeze generator
        if not opt.freezeG or (opt.freezeG and epoch <= freezeEpochs):
            g_optimizer.step()

        logger.log(d_error_total, g_err, epoch, n_batch, len(dataloader), d_real, d_fake_1, d_fake_2)

        if n_batch % 10 == 0:
            print('[%d/%d][%d/%d] Loss_D: %.4f Loss_G: %.4f D(x): %.4f D(G(z)): %.4f / %.4f'
                  % (epoch, opt.epochs, n_batch, len(dataloader),
                     d_error_total, g_err.item(), d_real, d_fake_1, d_fake_2))

        if n_batch % 100 == 0:
            Logger.batch = n_batch
            # generate fake with fixed noise
            test_fake = generator(fixed_noise)
            test_fake = F.pad(test_fake, (p, p, p, p), mode='replicate')

            # clone network to remove batch norm for relevance propagation
            canonical = type(discriminator)(nc, ndf, alpha, ngpu)
            canonical.load_state_dict(discriminator.state_dict())
            canonical.passBatchNormParametersToConvolution()
            canonical.removeBatchNormLayers()
            canonical.eval()

            # set ngpu to one, so relevance propagation works
            if (opt.ngpu > 1):
                canonical.setngpu(1)

            test_result, test_prob = canonical(test_fake)
            fake_double_check = discriminator(test_fake)
            discriminator.eval()
            _, fake_tripple_check = discriminator(test_fake)
            discriminator.train()

            test_relevance = canonical.relprop()

            # Relevance propagation on real image
            real_test = real_test[0].clone().unsqueeze(0)
            real_test = F.pad(real_test, (p, p, p, p), mode='replicate')

            real_test.requires_grad = True
            real_test_result, real_test_prob = canonical(real_test)
            real_doublecheck_prop = discriminator(real_test)
            discriminator.eval()
            _, real_tripplecheck_prop = discriminator(real_test)
            discriminator.train()
            real_test_relevance = canonical.relprop()
            del canonical

            # Add up relevance of all color channels
            test_relevance = torch.sum(test_relevance, 1, keepdim=True)
            real_test_relevance = torch.sum(real_test_relevance, 1, keepdim=True)

            bp = p
            test_fake_cat = torch.cat((test_fake[:, :, bp:-bp, bp:-bp], real_test[:, :, bp:-bp, bp:-bp]))
            test_relevance_cat = torch.cat(
                (test_relevance[:, :, bp:-bp, bp:-bp], real_test_relevance[:, :, bp:-bp, bp:-bp]))

            printdata = {'test_prob': test_prob.item(), 'real_test_prob': real_test_prob.item(),
                         'test_result': fake_double_check.item(), 'real_test_result': real_doublecheck_prop.item(),
                         # 'min_test_rel': torch.min(test_relevance), 'max_test_rel': torch.max(test_relevance),
                         'min_test_rel': fake_tripple_check.item(), 'max_test_rel': torch.max(test_relevance),
                         # 'min_real_rel': torch.min(real_test_relevance), 'max_real_rel': torch.max(real_test_relevance)}
                         'min_real_rel': real_tripplecheck_prop.item(), 'max_real_rel': torch.max(real_test_relevance)}

            if not opt.cont:
                log_epoch = epoch
            else:
                log_epoch = epoch + opt.cont

            img_name = logger.log_images(
                test_fake_cat.detach(), test_relevance_cat.detach(), test_fake.size(0),
                log_epoch, n_batch, len(dataloader), printdata, noLabel=opt.nolabel
            )

            # show images inline
            if opt.imgcat:
                comment = '{:.4f}-{:.4f}'.format(printdata['test_prob'], printdata['real_test_prob'])
                subprocess.call([os.path.expanduser('~/.iterm2/imgcat'),
                                 outf + '/' + opt.dataset + '/epoch_' + str(epoch) + '_batch_' + str(n_batch) + '_' + comment + '.png'])

            status = logger.display_status(epoch, opt.epochs, n_batch, len(dataloader), d_error_total, g_err,
                                           prediction_real, prediction_fake)

    Logger.epoch += 1

    # do checkpointing
    torch.save(generator.state_dict(), '%s/generator_epoch_{}.pth'.format(str(log_epoch)) % (checkpointdir))
    torch.save(discriminator.state_dict(), '%s/discriminator_epoch_{}.pth'.format(str(log_epoch)) % (checkpointdir))

    dic_compare = torch.load('%s/discriminator_epoch_{}.pth'.format(str(log_epoch)) % (checkpointdir),
                             map_location='cuda:0' if torch.cuda.is_available() else 'cpu')


    def compare_models(dict1, dict2):
        models_differ = 0
        for key_item_1, key_item_2 in zip(dict1.items(), dict2.items()):
            if torch.equal(key_item_1[1], key_item_2[1]):
                pass
            else:
                models_differ += 1
                if (key_item_1[0] == key_item_2[0]):
                    print('Mismtach found at', key_item_1[0])
                else:
                    raise Exception
        if models_differ == 0:
            print('Models match perfectly! :)')

    compare_models(discriminator.state_dict(), dic_compare)
