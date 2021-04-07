import os
import json
import multiprocessing
from random import random
import math
from math import log2, floor
from functools import partial
from contextlib import contextmanager, ExitStack
from pathlib import Path
from shutil import rmtree

import torch
from torch.cuda.amp import autocast, GradScaler
from torch.optim import Adam
from torch import nn, einsum
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torch.autograd import grad as torch_grad
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP

from PIL import Image
import torchvision
from torchvision import transforms
from kornia import filter2D

from transganformer.diff_augment import DiffAugment
from transganformer.version import __version__

from tqdm import tqdm
from einops import rearrange, reduce, repeat
from einops.layers.torch import Rearrange

from halonet_pytorch import HaloAttention

# asserts

assert torch.cuda.is_available(), 'You need to have an Nvidia GPU with CUDA installed.'

# constants

NUM_CORES = multiprocessing.cpu_count()
EXTS = ['jpg', 'jpeg', 'png']

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

@contextmanager
def null_context():
    yield

def combine_contexts(contexts):
    @contextmanager
    def multi_contexts():
        with ExitStack() as stack:
            yield [stack.enter_context(ctx()) for ctx in contexts]
    return multi_contexts

def is_power_of_two(val):
    return log2(val).is_integer()

def default(val, d):
    return val if exists(val) else d

def set_requires_grad(model, bool):
    for p in model.parameters():
        p.requires_grad = bool

def cycle(iterable):
    while True:
        for i in iterable:
            yield i

def raise_if_nan(t):
    if torch.isnan(t):
        raise NanException

def gradient_accumulate_contexts(gradient_accumulate_every, is_ddp, ddps):
    if is_ddp:
        num_no_syncs = gradient_accumulate_every - 1
        head = [combine_contexts(map(lambda ddp: ddp.no_sync, ddps))] * num_no_syncs
        tail = [null_context]
        contexts =  head + tail
    else:
        contexts = [null_context] * gradient_accumulate_every

    for context in contexts:
        with context():
            yield

def evaluate_in_chunks(max_batch_size, model, *args):
    split_args = list(zip(*list(map(lambda x: x.split(max_batch_size, dim=0), args))))
    chunked_outputs = [model(*i) for i in split_args]
    if len(chunked_outputs) == 1:
        return chunked_outputs[0]
    return torch.cat(chunked_outputs, dim=0)

def slerp(val, low, high):
    low_norm = low / torch.norm(low, dim=1, keepdim=True)
    high_norm = high / torch.norm(high, dim=1, keepdim=True)
    omega = torch.acos((low_norm * high_norm).sum(1))
    so = torch.sin(omega)
    res = (torch.sin((1.0 - val) * omega) / so).unsqueeze(1) * low + (torch.sin(val * omega) / so).unsqueeze(1) * high
    return res

def safe_div(n, d):
    try:
        res = n / d
    except ZeroDivisionError:
        prefix = '' if int(n >= 0) else '-'
        res = float(f'{prefix}inf')
    return res

# loss functions

def gen_hinge_loss(fake, real):
    return fake.mean()

def hinge_loss(real, fake):
    return (F.relu(1 + real) + F.relu(1 - fake)).mean()

def dual_contrastive_loss(real_logits, fake_logits):
    device = real_logits.device
    real_logits, fake_logits = map(lambda t: rearrange(t, '... -> (...)'), (real_logits, fake_logits))

    def loss_half(t1, t2):
        t1 = rearrange(t1, 'i -> i ()')
        t2 = repeat(t2, 'j -> i j', i = t1.shape[0])
        t = torch.cat((t1, t2), dim = -1)
        return F.cross_entropy(t, torch.zeros(t1.shape[0], device = device, dtype = torch.long))

    return loss_half(real_logits, fake_logits) + loss_half(-fake_logits, -real_logits)

# helper classes

class NanException(Exception):
    pass

class EMA():
    def __init__(self, beta):
        super().__init__()
        self.beta = beta
    def update_average(self, old, new):
        if not exists(old):
            return new
        return old * self.beta + (1 - self.beta) * new

class RandomApply(nn.Module):
    def __init__(self, prob, fn, fn_else = lambda x: x):
        super().__init__()
        self.fn = fn
        self.fn_else = fn_else
        self.prob = prob
    def forward(self, x):
        fn = self.fn if random() < self.prob else self.fn_else
        return fn(x)

class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(x, **kwargs) + x

# attention and transformer modules

class ChanNorm(nn.Module):
    def __init__(self, dim, eps = 1e-5):
        super().__init__()
        self.eps = eps
        self.g = nn.Parameter(torch.ones(1, dim, 1, 1))
        self.b = nn.Parameter(torch.zeros(1, dim, 1, 1))

    def forward(self, x):
        std = torch.var(x, dim = 1, unbiased = False, keepdim = True).sqrt()
        mean = torch.mean(x, dim = 1, keepdim = True)
        return (x - mean) / (std + self.eps) * self.g + self.b

class PreNorm(nn.Module):
    def __init__(self, dim, fn, dim_context = None):
        super().__init__()
        self.norm = ChanNorm(dim)
        self.norm_context = ChanNorm(dim_context) if exists(dim_context) else None
        self.fn = fn

    def forward(self, x, **kwargs):
        x = self.norm(x)

        if exists(self.norm_context):
            context = kwargs.pop('context')
            context = self.norm_context(context)
            kwargs.update(context = context)

        return self.fn(x, **kwargs)

class DepthWiseConv2d(nn.Module):
    def __init__(self, dim_in, dim_out, kernel_size, padding = 0, stride = 1, bias = True):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(dim_in, dim_in, kernel_size = kernel_size, padding = padding, groups = dim_in, stride = stride, bias = bias),
            nn.Conv2d(dim_in, dim_out, kernel_size = 1, bias = bias)
        )
    def forward(self, x):
        return self.net(x)

def FeedForward(dim, mult = 2, kernel_size = 1):
    padding = kernel_size // 2
    return nn.Sequential(
        nn.Conv2d(dim, dim * mult, kernel_size, padding = padding),
        nn.GELU(),
        nn.Conv2d(dim * mult, dim, kernel_size, padding = padding)
    )

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        dim_out = None,
        kv_dim = None,
        heads = 8,
        dim_head = 64,
        q_kernel_size = 1,
        kv_kernel_size = 3,
        out_kernel_size = 1,
        q_stride = 1
    ):
        super().__init__()
        inner_dim = dim_head *  heads
        kv_dim = default(kv_dim, dim)
        dim_out = default(dim_out, dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        q_padding = q_kernel_size // 2
        kv_padding = kv_kernel_size // 2
        out_padding = out_kernel_size // 2

        self.to_q = nn.Conv2d(dim, inner_dim, q_kernel_size, stride = q_stride, padding = q_padding, bias = False)
        self.to_kv = nn.Conv2d(kv_dim, inner_dim * 2, kv_kernel_size, padding = kv_padding, bias = False)

        self.to_out = nn.Sequential(
            nn.GELU(),
            nn.Conv2d(inner_dim, dim_out, out_kernel_size, padding = out_padding)
        )

    def forward(self, x, context = None):
        b, n, _, y, h, device = *x.shape, self.heads, x.device
        context = default(context, x)

        qkv = (self.to_q(x), *self.to_kv(context).chunk(2, dim = 1))

        out_h, out_w = qkv[0].shape[-2:]

        q, k, v = map(lambda t: rearrange(t, 'b (h d) x y -> (b h) (x y) d', h = h), qkv)

        dots = torch.einsum('b i d, b j d -> b i j', q, k) * self.scale

        attn = dots.softmax(dim=-1)

        out = torch.einsum('b i j, b j d -> b i d', attn, v)
        out = rearrange(out, '(b h) (x y) d -> b (h d) x y', h = h, x = out_h, y = out_w)
        return self.to_out(out)

class LinearAttention(nn.Module):
    def __init__(self, dim, dim_head = 64, heads = 8):
        super().__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        inner_dim = dim_head * heads

        self.to_q = nn.Conv2d(dim, inner_dim, 1, bias = False)
        self.to_kv = DepthWiseConv2d(dim, inner_dim * 2, 3, padding = 1, bias = False)
        self.to_out = nn.Sequential(
            nn.GELU(),
            nn.Conv2d(inner_dim, dim, 1)
        )

    def forward(self, fmap):
        h, x, y = self.heads, *fmap.shape[-2:]
        q, k, v = (self.to_q(fmap), *self.to_kv(fmap).chunk(2, dim = 1))
        q, k, v = map(lambda t: rearrange(t, 'b (h c) x y -> (b h) (x y) c', h = h), (q, k, v))

        q = q.softmax(dim = -1)
        k = k.softmax(dim = -2)

        q = q * self.scale

        context = einsum('b n d, b n e -> b d e', k, v)
        out = einsum('b n d, b d e -> b n e', q, context)
        out = rearrange(out, '(b h) (x y) d -> b (h d) x y', h = h, x = x, y = y)

        return self.to_out(out)

class ResFilmUpdate(nn.Module):
    def __init__(self, fn, eps = 1e-5):
        super().__init__()
        self.eps = eps
        self.fn = fn

    def forward(self, x, **kwargs):
        res = x
        out = self.fn(x, **kwargs)

        std = torch.var(res, dim = 1, unbiased = False, keepdim = True).sqrt()
        mean = torch.mean(res, dim = 1, keepdim = True)
        norm_res = (res - mean) / (std + self.eps)

        g, b = out.chunk(2, dim = 1)
        return norm_res * g + b

# dataset

def convert_image_to(img_type, image):
    if image.mode != img_type:
        return image.convert(img_type)
    return image

class identity(object):
    def __call__(self, tensor):
        return tensor

class expand_greyscale(object):
    def __init__(self, transparent):
        self.transparent = transparent

    def __call__(self, tensor):
        channels = tensor.shape[0]
        num_target_channels = 4 if self.transparent else 3

        if channels == num_target_channels:
            return tensor

        alpha = None
        if channels == 1:
            color = tensor.expand(3, -1, -1)
        elif channels == 2:
            color = tensor[:1].expand(3, -1, -1)
            alpha = tensor[1:]
        else:
            raise Exception(f'image with invalid number of channels given {channels}')

        if not exists(alpha) and self.transparent:
            alpha = torch.ones(1, *tensor.shape[1:], device=tensor.device)

        return color if not self.transparent else torch.cat((color, alpha))

def resize_to_minimum_size(min_size, image):
    if max(*image.size) < min_size:
        return torchvision.transforms.functional.resize(image, min_size)
    return image

class ImageDataset(Dataset):
    def __init__(
        self,
        folder,
        image_size,
        transparent = False,
        greyscale = False,
        aug_prob = 0.
    ):
        super().__init__()
        self.folder = folder
        self.image_size = image_size
        self.paths = [p for ext in EXTS for p in Path(f'{folder}').glob(f'**/*.{ext}')]
        assert len(self.paths) > 0, f'No images were found in {folder} for training'

        if transparent:
            num_channels = 4
            pillow_mode = 'RGBA'
            expand_fn = expand_greyscale(transparent)
        elif greyscale:
            num_channels = 1
            pillow_mode = 'L'
            expand_fn = identity()
        else:
            num_channels = 3
            pillow_mode = 'RGB'
            expand_fn = expand_greyscale(transparent)

        convert_image_fn = partial(convert_image_to, pillow_mode)

        self.transform = transforms.Compose([
            transforms.Lambda(convert_image_fn),
            transforms.Lambda(partial(resize_to_minimum_size, image_size)),
            transforms.Resize(image_size),
            RandomApply(aug_prob, transforms.RandomResizedCrop(image_size, scale=(0.5, 1.0), ratio=(0.98, 1.02)), transforms.CenterCrop(image_size)),
            transforms.ToTensor(),
            transforms.Lambda(expand_fn)
        ])

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, index):
        path = self.paths[index]
        img = Image.open(path)
        return self.transform(img)

# augmentations

def random_hflip(tensor, prob):
    if prob > random():
        return tensor
    return torch.flip(tensor, dims=(3,))

class AugWrapper(nn.Module):
    def __init__(self, D, image_size):
        super().__init__()
        self.D = D

    def forward(self, images, prob = 0., types = [], detach = False, **kwargs):
        context = torch.no_grad if detach else null_context

        with context():
            if random() < prob:
                images = random_hflip(images, prob=0.5)
                images = DiffAugment(images, types=types)

        return self.D(images, **kwargs)

# modifiable global variables

def upsample(scale_factor = 2):
    return nn.Upsample(scale_factor = scale_factor)

# activation

def leaky_relu(p = 0.1):
    return nn.LeakyReLU(p)

# mapping network

class EqualLinear(nn.Module):
    def __init__(self, in_dim, out_dim, lr_mul = 1, bias = True):
        super().__init__()
        self.weight = nn.Parameter(torch.randn(out_dim, in_dim))
        if bias:
            self.bias = nn.Parameter(torch.zeros(out_dim))

        self.lr_mul = lr_mul

    def forward(self, input):
        return F.linear(input, self.weight * self.lr_mul, bias=self.bias * self.lr_mul)

class MappingNetwork(nn.Module):
    def __init__(self, dim, depth, lr_mul = 0.1):
        super().__init__()

        layers = []
        for i in range(depth):
            layers.extend([EqualLinear(dim, dim, lr_mul), leaky_relu()])

        self.net = nn.Sequential(
            *layers,
            nn.Linear(dim, dim * 4)
        )

    def forward(self, x):
        x = F.normalize(x, dim=1)
        x = self.net(x)
        return rearrange(x, 'b (c h w) -> b c h w', h = 2, w = 2)

# generative adversarial network

class Generator(nn.Module):
    def __init__(
        self,
        *,
        image_size,
        latent_dim = 256,
        fmap_max = 512,
        fmap_inverse_coef = 12,
        init_channel = 3,
        mapping_network_depth = 4
    ):
        super().__init__()
        assert is_power_of_two(image_size), 'image size must be a power of 2'
        num_layers = int(log2(image_size)) - 1
        
        self.mapping = MappingNetwork(latent_dim, mapping_network_depth)
        self.initial_block = nn.Parameter(torch.randn((latent_dim, 4, 4)))

        self.layers = nn.ModuleList([])

        fmap_size = 4
        chan = latent_dim
        min_chan = 16

        for ind in range(num_layers):
            is_last = ind == (num_layers - 1)

            if not is_last:
                fmap_size *= 2

                chan_out = max(min_chan, chan // 2)

                upsample = nn.Sequential(
                    DepthWiseConv2d(chan, chan_out * 4, 3, padding = 1),
                    nn.PixelShuffle(2)
                )

                chan = chan_out
            else:
                upsample = nn.Identity()

            attn_class = Attention if fmap_size <= 16 else partial(HaloAttention, block_size = 16, halo_size = 4)

            self.layers.append(nn.ModuleList([
                upsample,
                Residual(PreNorm(chan, attn_class(dim = chan))),
                ResFilmUpdate(PreNorm(chan, Attention(chan, kv_dim = latent_dim, dim_out = chan * 2), dim_context = latent_dim)),
                ResFilmUpdate(PreNorm(latent_dim, Attention(latent_dim, kv_dim = chan, dim_out = latent_dim * 2), dim_context = chan)),
                Residual(PreNorm(chan, FeedForward(chan))),
            ]))

        self.to_img = nn.Sequential(
            nn.Conv2d(chan, init_channel, 3, padding = 1)
        )

    def forward(self, x):
        b = x.shape[0]

        latents = self.mapping(x)
        fmap = repeat(self.initial_block, 'c h w -> b c h w', b = b)

        for upsample, attn, fmap_to_latents_attn, latents_to_fmap_attn, ff in self.layers:
            fmap = upsample(fmap)
            fmap = attn(fmap)

            if exists(fmap_to_latents_attn):
                fmap = fmap_to_latents_attn(fmap, context = latents)

            if exists(latents_to_fmap_attn):
                latents = latents_to_fmap_attn(latents, context = fmap)

            fmap = ff(fmap)

        return self.to_img(fmap)

class SimpleDecoder(nn.Module):
    def __init__(
        self,
        *,
        chan_in,
        chan_out = 3,
        num_upsamples = 4,
    ):
        super().__init__()

        layers = nn.ModuleList([])
        final_chan = chan_out
        chans = chan_in

        for ind in range(num_upsamples):
            last_layer = ind == (num_upsamples - 1)
            chan_out = chans if not last_layer else final_chan * 2
            layer = nn.Sequential(
                upsample(),
                nn.Conv2d(chans, chan_out, 3, padding = 1),
                nn.GLU(dim = 1)
            )
            layers.append(layer)
            chans //= 2

        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)

class Discriminator(nn.Module):
    def __init__(
        self,
        *,
        image_size,
        fmap_max = 256,
        fmap_inverse_coef = 12,
        init_channel = 3,
    ):
        super().__init__()
        assert is_power_of_two(image_size), 'image size must be a power of 2'
        num_layers = int(log2(image_size)) - 1

        self.conv_embed = nn.Conv2d(init_channel, fmap_max, kernel_size = 7, stride = 2, padding = 3)

        image_size //= 2

        self.image_sizes = []
        self.layers = nn.ModuleList([])
        for ind in range(num_layers):
            image_size //= 2
            self.image_sizes.append(image_size)

            attn_class = Attention if image_size <= 16 else partial(HaloAttention, block_size = 8, halo_size = 4)

            self.layers.append(nn.ModuleList([
                Residual(PreNorm(fmap_max, attn_class(dim = fmap_max))),
                DepthWiseConv2d(fmap_max, fmap_max, 3, stride = 2, padding = 1),
                Residual(PreNorm(fmap_max, FeedForward(dim = fmap_max)))
            ]))

        self.aux_decoder = SimpleDecoder(chan_in = fmap_max, chan_out = init_channel)

        self.to_logits = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            Rearrange('b c () () -> b c'),
            nn.Linear(fmap_max, 1),
            Rearrange('b () -> b')
        )

    def forward(self, x, calc_aux_loss = False):
        x_ = x
        x = self.conv_embed(x)

        fmaps = []

        for (attn, downsample, ff), image_size in zip(self.layers, self.image_sizes):
            x = attn(x)
            x = downsample(x)
            x = ff(x)

            fmaps.append(x)

        x = self.to_logits(x)

        if not calc_aux_loss:
            return x, None

        recon = self.aux_decoder(fmaps[2])
        recon_loss = F.mse_loss(x_, recon)
        return x, recon_loss

class Transganformer(nn.Module):
    def __init__(
        self,
        *,
        latent_dim,
        image_size,
        fmap_max = 512,
        fmap_inverse_coef = 12,
        transparent = False,
        greyscale = False,
        ttur_mult = 1.,
        lr = 2e-4,
        rank = 0,
        ddp = False
    ):
        super().__init__()
        self.latent_dim = latent_dim
        self.image_size = image_size

        if transparent:
            init_channel = 4
        elif greyscale:
            init_channel = 1
        else:
            init_channel = 3

        G_kwargs = dict(
            image_size = image_size,
            latent_dim = latent_dim,
            fmap_max = fmap_max,
            fmap_inverse_coef = fmap_inverse_coef,
            init_channel = init_channel
        )

        self.G = Generator(**G_kwargs)

        self.D = Discriminator(
            image_size = image_size,
            fmap_max = fmap_max,
            fmap_inverse_coef = fmap_inverse_coef,
            init_channel = init_channel
        )

        self.ema_updater = EMA(0.995)
        self.GE = Generator(**G_kwargs)
        set_requires_grad(self.GE, False)

        self.G_opt = Adam(self.G.parameters(), lr = lr, betas=(0.5, 0.9))
        self.D_opt = Adam(self.D.parameters(), lr = lr * ttur_mult, betas=(0.5, 0.9))

        self.apply(self._init_weights)
        self.reset_parameter_averaging()

        self.cuda(rank)
        self.D_aug = AugWrapper(self.D, image_size)

    def _init_weights(self, m):
        if type(m) in {nn.Conv2d, nn.Linear}:
            nn.init.kaiming_normal_(m.weight, a=0, mode='fan_in', nonlinearity='leaky_relu')

    def EMA(self):
        def update_moving_average(ma_model, current_model):
            for current_params, ma_params in zip(current_model.parameters(), ma_model.parameters()):
                old_weight, up_weight = ma_params.data, current_params.data
                ma_params.data = self.ema_updater.update_average(old_weight, up_weight)

            for current_buffer, ma_buffer in zip(current_model.buffers(), ma_model.buffers()):
                new_buffer_value = self.ema_updater.update_average(ma_buffer, current_buffer)
                ma_buffer.copy_(new_buffer_value)

        update_moving_average(self.GE, self.G)

    def reset_parameter_averaging(self):
        self.GE.load_state_dict(self.G.state_dict())

    def forward(self, x):
        raise NotImplemented

# trainer

class Trainer():
    def __init__(
        self,
        name = 'default',
        results_dir = 'results',
        models_dir = 'models',
        base_dir = './',
        num_workers = None,
        latent_dim = 256,
        image_size = 128,
        num_image_tiles = 8,
        fmap_max = 512,
        transparent = False,
        greyscale = False,
        batch_size = 4,
        gp_weight = 10,
        gradient_accumulate_every = 1,
        dual_contrast_loss = False,
        lr = 2e-4,
        lr_mlp = 1.,
        ttur_mult = 1.,
        save_every = 1000,
        evaluate_every = 1000,
        aug_prob = None,
        aug_types = ['translation', 'cutout'],
        dataset_aug_prob = 0.,
        calculate_fid_every = None,
        calculate_fid_num_images = 12800,
        clear_fid_cache = False,
        is_ddp = False,
        rank = 0,
        world_size = 1,
        log = False,
        amp = False,
        *args,
        **kwargs
    ):
        self.GAN_params = [args, kwargs]
        self.GAN = None

        self.name = name

        base_dir = Path(base_dir)
        self.base_dir = base_dir
        self.results_dir = base_dir / results_dir
        self.models_dir = base_dir / models_dir
        self.fid_dir = base_dir / 'fid' / name

        self.config_path = self.models_dir / name / '.config.json'

        assert is_power_of_two(image_size), 'image size must be a power of 2 (32, 64, 128, 256, 512, 1024)'

        self.image_size = image_size
        self.num_image_tiles = num_image_tiles

        self.latent_dim = latent_dim
        self.fmap_max = fmap_max
        self.transparent = transparent
        self.greyscale = greyscale

        assert (int(self.transparent) + int(self.greyscale)) < 2, 'you can only set either transparency or greyscale'

        self.aug_prob = aug_prob
        self.aug_types = aug_types

        self.lr = lr
        self.num_workers = num_workers
        self.ttur_mult = ttur_mult
        self.batch_size = batch_size
        self.gradient_accumulate_every = gradient_accumulate_every

        self.gp_weight = gp_weight

        self.evaluate_every = evaluate_every
        self.save_every = save_every
        self.steps = 0

        self.dual_contrast_loss = dual_contrast_loss

        self.d_loss = 0
        self.g_loss = 0
        self.last_gp_loss = None
        self.last_recon_loss = None
        self.last_fid = None

        self.init_folders()

        self.loader = None
        self.dataset_aug_prob = dataset_aug_prob

        self.calculate_fid_every = calculate_fid_every
        self.calculate_fid_num_images = calculate_fid_num_images
        self.clear_fid_cache = clear_fid_cache

        self.is_ddp = is_ddp
        self.is_main = rank == 0
        self.rank = rank
        self.world_size = world_size

        self.amp = amp
        self.G_scaler = GradScaler(enabled = self.amp)
        self.D_scaler = GradScaler(enabled = self.amp)

    @property
    def image_extension(self):
        return 'jpg' if not self.transparent else 'png'

    @property
    def checkpoint_num(self):
        return floor(self.steps // self.save_every)
        
    def init_GAN(self):
        args, kwargs = self.GAN_params

        # instantiate GAN

        self.GAN = Transganformer(
            lr = self.lr,
            latent_dim = self.latent_dim,
            image_size = self.image_size,
            ttur_mult = self.ttur_mult,
            fmap_max = self.fmap_max,
            transparent = self.transparent,
            greyscale = self.greyscale,
            rank = self.rank,
            *args,
            **kwargs
        )

        if self.is_ddp:
            ddp_kwargs = {'device_ids': [self.rank], 'output_device': self.rank, 'find_unused_parameters': True}

            self.G_ddp = DDP(self.GAN.G, **ddp_kwargs)
            self.D_ddp = DDP(self.GAN.D, **ddp_kwargs)
            self.D_aug_ddp = DDP(self.GAN.D_aug, **ddp_kwargs)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config()))

    def load_config(self):
        config = self.config() if not self.config_path.exists() else json.loads(self.config_path.read_text())
        self.image_size = config['image_size']
        self.transparent = config['transparent']
        self.greyscale = config.pop('greyscale', False)
        self.fmap_max = config.pop('fmap_max', 512)
        del self.GAN
        self.init_GAN()

    def config(self):
        return {
            'image_size': self.image_size,
            'transparent': self.transparent,
            'greyscale': self.greyscale
        }

    def set_data_src(self, folder):
        num_workers = default(self.num_workers, math.ceil(NUM_CORES / self.world_size))
        self.dataset = ImageDataset(folder, self.image_size, transparent = self.transparent, greyscale = self.greyscale, aug_prob = self.dataset_aug_prob)
        sampler = DistributedSampler(self.dataset, rank=self.rank, num_replicas=self.world_size, shuffle=True) if self.is_ddp else None
        dataloader = DataLoader(self.dataset, num_workers = num_workers, batch_size = math.ceil(self.batch_size / self.world_size), sampler = sampler, shuffle = not self.is_ddp, drop_last = True, pin_memory = True)
        self.loader = cycle(dataloader)

        # auto set augmentation prob for user if dataset is detected to be low
        num_samples = len(self.dataset)
        if not exists(self.aug_prob) and num_samples < 1e5:
            self.aug_prob = min(0.5, (1e5 - num_samples) * 3e-6)
            print(f'autosetting augmentation probability to {round(self.aug_prob * 100)}%')

    def train(self):
        assert exists(self.loader), 'You must first initialize the data source with `.set_data_src(<folder of images>)`'
        device = torch.device(f'cuda:{self.rank}')

        if not exists(self.GAN):
            self.init_GAN()

        self.GAN.train()
        total_disc_loss = torch.zeros([], device=device)
        total_gen_loss = torch.zeros([], device=device)

        batch_size = math.ceil(self.batch_size / self.world_size)

        image_size = self.GAN.image_size
        latent_dim = self.GAN.latent_dim

        aug_prob   = default(self.aug_prob, 0)
        aug_types  = self.aug_types
        aug_kwargs = {'prob': aug_prob, 'types': aug_types}

        G = self.GAN.G if not self.is_ddp else self.G_ddp
        D = self.GAN.D if not self.is_ddp else self.D_ddp
        D_aug = self.GAN.D_aug if not self.is_ddp else self.D_aug_ddp

        apply_gradient_penalty = self.steps % 4 == 0

        # amp related contexts and functions

        amp_context = autocast if self.amp else null_context

        # discriminator loss fn

        if self.dual_contrast_loss:
            D_loss_fn = dual_contrastive_loss
        else:
            D_loss_fn = hinge_loss

        # train discriminator

        self.GAN.D_opt.zero_grad()
        for i in gradient_accumulate_contexts(self.gradient_accumulate_every, self.is_ddp, ddps=[D_aug, G]):
            latents = torch.randn(batch_size, latent_dim).cuda(self.rank)
            image_batch = next(self.loader).cuda(self.rank)
            image_batch.requires_grad_()

            with amp_context():
                with torch.no_grad():
                    generated_images = G(latents)

                fake_output, _ = D_aug(generated_images, detach = True, **aug_kwargs)

                real_output, real_aux_loss = D_aug(image_batch,  calc_aux_loss = True, **aug_kwargs)

                real_output_loss = real_output
                fake_output_loss = fake_output

                divergence = D_loss_fn(real_output_loss, fake_output_loss)
                disc_loss = divergence

                aux_loss = real_aux_loss
                disc_loss = disc_loss + aux_loss

            if apply_gradient_penalty:
                outputs = [real_output]
                outputs = list(map(self.D_scaler.scale, outputs)) if self.amp else outputs

                scaled_gradients = torch_grad(outputs=outputs, inputs=image_batch,
                                       grad_outputs=list(map(lambda t: torch.ones(t.size(), device = image_batch.device), outputs)),
                                       create_graph=True, retain_graph=True, only_inputs=True)[0]

                inv_scale = safe_div(1., self.D_scaler.get_scale()) if self.amp else 1.

                if inv_scale != float('inf'):
                    gradients = scaled_gradients * inv_scale

                    with amp_context():
                        gradients = gradients.reshape(batch_size, -1)
                        gp =  self.gp_weight * ((gradients.norm(2, dim=1) - 1) ** 2).mean()

                        if not torch.isnan(gp):
                            disc_loss = disc_loss + gp
                            self.last_gp_loss = gp.clone().detach().item()

            with amp_context():
                disc_loss = disc_loss / self.gradient_accumulate_every

            disc_loss.register_hook(raise_if_nan)
            self.D_scaler.scale(disc_loss).backward()
            total_disc_loss += divergence

        self.last_recon_loss = aux_loss.item()
        self.d_loss = float(total_disc_loss.item() / self.gradient_accumulate_every)
        self.D_scaler.step(self.GAN.D_opt)
        self.D_scaler.update()

        # generator loss fn

        if self.dual_contrast_loss:
            G_loss_fn = dual_contrastive_loss
            G_requires_calc_real = True
        else:
            G_loss_fn = gen_hinge_loss
            G_requires_calc_real = False

        # train generator

        self.GAN.G_opt.zero_grad()

        for i in gradient_accumulate_contexts(self.gradient_accumulate_every, self.is_ddp, ddps=[G, D_aug]):
            latents = torch.randn(batch_size, latent_dim).cuda(self.rank)

            if G_requires_calc_real:
                image_batch = next(self.loader).cuda(self.rank)
                image_batch.requires_grad_()

            with amp_context():
                generated_images = G(latents)

                fake_output, _ = D_aug(generated_images, **aug_kwargs)
                real_output, _ = D_aug(image_batch, **aug_kwargs) if G_requires_calc_real else (None, None)

                loss = G_loss_fn(fake_output, real_output)

                gen_loss = loss

                gen_loss = gen_loss / self.gradient_accumulate_every

            gen_loss.register_hook(raise_if_nan)
            self.G_scaler.scale(gen_loss).backward()
            total_gen_loss += loss 

        self.g_loss = float(total_gen_loss.item() / self.gradient_accumulate_every)
        self.G_scaler.step(self.GAN.G_opt)
        self.G_scaler.update()

        # calculate moving averages

        if self.is_main and self.steps % 10 == 0 and self.steps > 20000:
            self.GAN.EMA()

        if self.is_main and self.steps <= 25000 and self.steps % 1000 == 2:
            self.GAN.reset_parameter_averaging()

        # save from NaN errors

        if any(torch.isnan(l) for l in (total_gen_loss, total_disc_loss)):
            print(f'NaN detected for generator or discriminator. Loading from checkpoint #{self.checkpoint_num}')
            self.load(self.checkpoint_num)
            raise NanException

        del total_disc_loss
        del total_gen_loss

        # periodically save results

        if self.is_main:
            if self.steps % self.save_every == 0:
                self.save(self.checkpoint_num)

            if self.steps % self.evaluate_every == 0 or (self.steps % 100 == 0 and self.steps < 20000):
                self.evaluate(floor(self.steps / self.evaluate_every), num_image_tiles = self.num_image_tiles)

            if exists(self.calculate_fid_every) and self.steps % self.calculate_fid_every == 0 and self.steps != 0:
                num_batches = math.ceil(self.calculate_fid_num_images / self.batch_size)
                fid = self.calculate_fid(num_batches)
                self.last_fid = fid

                with open(str(self.results_dir / self.name / f'fid_scores.txt'), 'a') as f:
                    f.write(f'{self.steps},{fid}\n')

        self.steps += 1

    @torch.no_grad()
    def evaluate(self, num = 0, num_image_tiles = 4):
        self.GAN.eval()

        ext = self.image_extension
        num_rows = num_image_tiles
    
        latent_dim = self.GAN.latent_dim
        image_size = self.GAN.image_size

        # latents and noise

        latents = torch.randn((num_rows ** 2, latent_dim)).cuda(self.rank)

        # regular

        generated_images = self.generate_(self.GAN.G, latents)
        torchvision.utils.save_image(generated_images, str(self.results_dir / self.name / f'{str(num)}.{ext}'), nrow=num_rows)
        
        # moving averages

        generated_images = self.generate_(self.GAN.GE, latents)
        torchvision.utils.save_image(generated_images, str(self.results_dir / self.name / f'{str(num)}-ema.{ext}'), nrow=num_rows)

    @torch.no_grad()
    def generate(self, num=0, num_image_tiles=4, checkpoint=None, types=['default', 'ema']):
        self.GAN.eval()

        latent_dim = self.GAN.latent_dim
        dir_name = self.name + str('-generated-') + str(checkpoint)
        dir_full = Path().absolute() / self.results_dir / dir_name
        ext = self.image_extension

        if not dir_full.exists():
            os.mkdir(dir_full)

        # regular
        if 'default' in types:
            for i in tqdm(range(num_image_tiles), desc='Saving generated default images'):
                latents = torch.randn((1, latent_dim)).cuda(self.rank)
                generated_image = self.generate_(self.GAN.G, latents)
                path = str(self.results_dir / dir_name / f'{str(num)}-{str(i)}.{ext}')
                torchvision.utils.save_image(generated_image[0], path, nrow=1)

        # moving averages
        if 'ema' in types:
            for i in tqdm(range(num_image_tiles), desc='Saving generated EMA images'):
                latents = torch.randn((1, latent_dim)).cuda(self.rank)
                generated_image = self.generate_(self.GAN.GE, latents)
                path = str(self.results_dir / dir_name / f'{str(num)}-{str(i)}-ema.{ext}')
                torchvision.utils.save_image(generated_image[0], path, nrow=1)

        return dir_full

    @torch.no_grad()
    def show_progress(self, num_images=4, types=['default', 'ema']):
        checkpoints = self.get_checkpoints()
        assert exists(checkpoints), 'cannot find any checkpoints to create a training progress video for'

        dir_name = self.name + str('-progress')
        dir_full = Path().absolute() / self.results_dir / dir_name
        ext = self.image_extension
        latents = None

        zfill_length = math.ceil(math.log10(len(checkpoints)))

        if not dir_full.exists():
            os.mkdir(dir_full)

        for checkpoint in tqdm(checkpoints, desc='Generating progress images'):
            self.load(checkpoint, print_version=False)
            self.GAN.eval()

            if checkpoint == 0:
                latents = torch.randn((num_images, self.GAN.latent_dim)).cuda(self.rank)

            # regular
            if 'default' in types:
                generated_image = self.generate_(self.GAN.G, latents)
                path = str(self.results_dir / dir_name / f'{str(checkpoint).zfill(zfill_length)}.{ext}')
                torchvision.utils.save_image(generated_image, path, nrow=num_images)

            # moving averages
            if 'ema' in types:
                generated_image = self.generate_(self.GAN.GE, latents)
                path = str(self.results_dir / dir_name / f'{str(checkpoint).zfill(zfill_length)}-ema.{ext}')
                torchvision.utils.save_image(generated_image, path, nrow=num_images)

    @torch.no_grad()
    def calculate_fid(self, num_batches):
        from pytorch_fid import fid_score
        torch.cuda.empty_cache()

        real_path = self.fid_dir / 'real'
        fake_path = self.fid_dir / 'fake'

        # remove any existing files used for fid calculation and recreate directories
        if not real_path.exists() or self.clear_fid_cache:
            rmtree(real_path, ignore_errors=True)
            os.makedirs(real_path)

            for batch_num in tqdm(range(num_batches), desc='calculating FID - saving reals'):
                real_batch = next(self.loader)
                for k, image in enumerate(real_batch.unbind(0)):
                    ind = k + batch_num * self.batch_size
                    torchvision.utils.save_image(image, real_path / f'{ind}.png')

        # generate a bunch of fake images in results / name / fid_fake

        rmtree(fake_path, ignore_errors=True)
        os.makedirs(fake_path)

        self.GAN.eval()
        ext = self.image_extension

        latent_dim = self.GAN.latent_dim
        image_size = self.GAN.image_size

        for batch_num in tqdm(range(num_batches), desc='calculating FID - saving generated'):
            # latents and noise
            latents = torch.randn(self.batch_size, latent_dim).cuda(self.rank)

            # moving averages
            generated_images = self.generate_(self.GAN.GE, latents)

            for j, image in enumerate(generated_images.unbind(0)):
                ind = j + batch_num * self.batch_size
                torchvision.utils.save_image(image, str(fake_path / f'{str(ind)}-ema.{ext}'))

        return fid_score.calculate_fid_given_paths([str(real_path), str(fake_path)], 256, latents.device, 2048)

    @torch.no_grad()
    def generate_(self, G, style, num_image_tiles = 8):
        generated_images = evaluate_in_chunks(self.batch_size, G, style)
        return generated_images.clamp_(0., 1.)

    @torch.no_grad()
    def generate_interpolation(self, num = 0, num_image_tiles = 8, num_steps = 100, save_frames = False):
        self.GAN.eval()
        ext = self.image_extension
        num_rows = num_image_tiles

        latent_dim = self.GAN.latent_dim
        image_size = self.GAN.image_size

        # latents and noise

        latents_low = torch.randn(num_rows ** 2, latent_dim).cuda(self.rank)
        latents_high = torch.randn(num_rows ** 2, latent_dim).cuda(self.rank)

        ratios = torch.linspace(0., 8., num_steps)

        frames = []
        for ratio in tqdm(ratios):
            interp_latents = slerp(ratio, latents_low, latents_high)
            generated_images = self.generate_(self.GAN.GE, interp_latents)
            images_grid = torchvision.utils.make_grid(generated_images, nrow = num_rows)
            pil_image = transforms.ToPILImage()(images_grid.cpu())
            
            if self.transparent:
                background = Image.new('RGBA', pil_image.size, (255, 255, 255))
                pil_image = Image.alpha_composite(background, pil_image)
                
            frames.append(pil_image)

        frames[0].save(str(self.results_dir / self.name / f'{str(num)}.gif'), save_all=True, append_images=frames[1:], duration=80, loop=0, optimize=True)

        if save_frames:
            folder_path = (self.results_dir / self.name / f'{str(num)}')
            folder_path.mkdir(parents=True, exist_ok=True)
            for ind, frame in enumerate(frames):
                frame.save(str(folder_path / f'{str(ind)}.{ext}'))

    def print_log(self):
        data = [
            ('G', self.g_loss),
            ('D', self.d_loss),
            ('GP', self.last_gp_loss),
            ('SS', self.last_recon_loss),
            ('FID', self.last_fid)
        ]

        data = [d for d in data if exists(d[1])]
        log = ' | '.join(map(lambda n: f'{n[0]}: {n[1]:.2f}', data))
        print(log)

    def model_name(self, num):
        return str(self.models_dir / self.name / f'model_{num}.pt')

    def init_folders(self):
        (self.results_dir / self.name).mkdir(parents=True, exist_ok=True)
        (self.models_dir / self.name).mkdir(parents=True, exist_ok=True)

    def clear(self):
        rmtree(str(self.models_dir / self.name), True)
        rmtree(str(self.results_dir / self.name), True)
        rmtree(str(self.fid_dir), True)
        rmtree(str(self.config_path), True)
        self.init_folders()

    def save(self, num):
        save_data = {
            'GAN': self.GAN.state_dict(),
            'version': __version__,
            'G_scaler': self.G_scaler.state_dict(),
            'D_scaler': self.D_scaler.state_dict()
        }

        torch.save(save_data, self.model_name(num))
        self.write_config()

    def load(self, num=-1, print_version=True):
        self.load_config()

        name = num
        if num == -1:
            checkpoints = self.get_checkpoints()

            if not exists(checkpoints):
                return

            name = checkpoints[-1]
            print(f'continuing from previous epoch - {name}')

        self.steps = name * self.save_every

        load_data = torch.load(self.model_name(name))

        if print_version and 'version' in load_data and self.is_main:
            print(f"loading from version {load_data['version']}")

        try:
            self.GAN.load_state_dict(load_data['GAN'])
        except Exception as e:
            print('unable to load save model. please try downgrading the package to the version specified by the saved model')
            raise e

        if 'G_scaler' in load_data:
            self.G_scaler.load_state_dict(load_data['G_scaler'])
        if 'D_scaler' in load_data:
            self.D_scaler.load_state_dict(load_data['D_scaler'])

    def get_checkpoints(self):
        file_paths = [p for p in Path(self.models_dir / self.name).glob('model_*.pt')]
        saved_nums = sorted(map(lambda x: int(x.stem.split('_')[1]), file_paths))

        if len(saved_nums) == 0:
            return None

        return saved_nums
