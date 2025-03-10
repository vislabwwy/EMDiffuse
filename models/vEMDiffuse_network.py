import math
from functools import partial
from inspect import isfunction

import numpy as np
import torch
from tqdm import tqdm

from core.base_network import BaseNetwork


class Network(BaseNetwork):
    def __init__(self, unet, beta_schedule, norm=True, module_name='sr3', **kwargs):
        super(Network, self).__init__(**kwargs)
        from .guided_diffusion_modules.unet_3d import UNet

        self.denoise_fn = UNet(**unet)
        self.beta_schedule = beta_schedule

        self.norm = norm

    def set_loss(self, loss_fn):
        self.loss_fn = loss_fn

    def set_new_noise_schedule(self, device=torch.device('cuda'), phase='train'):
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        to_torch = partial(torch.tensor, dtype=torch.float32, device=device)
        betas = make_beta_schedule(**self.beta_schedule[phase])
        betas = betas.detach().cpu().numpy() if isinstance(
            betas, torch.Tensor) else betas
        alphas = 1. - betas

        timesteps, = betas.shape
        self.num_timesteps = int(timesteps)

        gammas = np.cumprod(alphas, axis=0)
        gammas_prev = np.append(1., gammas[:-1])

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.register_buffer('gammas', to_torch(gammas))
        self.register_buffer('sqrt_recip_gammas', to_torch(np.sqrt(1. / gammas)))
        self.register_buffer('sqrt_recipm1_gammas', to_torch(np.sqrt(1. / gammas - 1)))

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        posterior_variance = betas * (1. - gammas_prev) / (1. - gammas)
        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
        self.register_buffer('posterior_log_variance_clipped', to_torch(np.log(np.maximum(posterior_variance, 1e-20))))
        self.register_buffer('posterior_mean_coef1', to_torch(betas * np.sqrt(gammas_prev) / (1. - gammas)))
        self.register_buffer('posterior_mean_coef2', to_torch((1. - gammas_prev) * np.sqrt(alphas) / (1. - gammas)))

    def predict_start_from_noise(self, y_t, t, noise):
        return (
                extract(self.sqrt_recip_gammas, t, y_t.shape) * y_t -
                extract(self.sqrt_recipm1_gammas, t, y_t.shape) * noise
        )

    def q_posterior(self, y_0_hat, y_t, t):
        posterior_mean = (
                extract(self.posterior_mean_coef1, t, y_t.shape) * y_0_hat +
                extract(self.posterior_mean_coef2, t, y_t.shape) * y_t
        )
        posterior_log_variance_clipped = extract(self.posterior_log_variance_clipped, t, y_t.shape)
        return posterior_mean, posterior_log_variance_clipped

    def p_mean_variance(self, y_t, t, channel_index, clip_denoised: bool, y_cond=None):
        noise_level = extract(self.gammas, t, x_shape=(1, 1)).to(y_t.device)
        y_0_hat = self.predict_start_from_noise(
            y_t, t=t, noise=self.denoise_fn(torch.cat([y_cond, y_t], dim=1), noise_level, channel_index))

        if clip_denoised:  # todo: clip

            y_0_hat.clamp_(-1., 1.)


        model_mean, posterior_log_variance = self.q_posterior(
            y_0_hat=y_0_hat, y_t=y_t, t=t, )
        return model_mean, posterior_log_variance, y_0_hat

    def q_sample(self, y_0, sample_gammas, noise=None):
        noise = default(noise, lambda: torch.randn_like(y_0))
        return (
                sample_gammas.sqrt() * y_0 +
                (1 - sample_gammas).sqrt() * noise
        )

    @torch.no_grad()
    def p_sample(self, y_t, t, channel_index, clip_denoised=True, y_cond=None, path=None, adjust=False):
        model_mean, model_log_variance, y_0_hat = self.p_mean_variance(
            y_t=y_t, t=t, clip_denoised=clip_denoised, y_cond=y_cond, channel_index=channel_index)

        noise = torch.randn_like(y_t) if any(t > 0) else torch.zeros_like(y_t)
        if adjust:
            if t[0] < (self.num_timesteps * 0.2):  # todo: optimize this code to accelerate
                mean_diff = model_mean.view(model_mean.size(0), -1).mean(1) - y_cond.view(y_cond.size(0), -1).mean(1)
                mean_diff = mean_diff.view(model_mean.size(0), 1, 1, 1)
                # print(mean_diff.shape, model_mean.shape, mean_diff.repeat((1, model_mean.shape[1], model_mean.shape[2], model_mean.shape[3])).shape)
                model_mean = model_mean - 0.5 * mean_diff.repeat(
                    (1, model_mean.shape[1], model_mean.shape[2], model_mean.shape[3]))

        return model_mean + noise * (0.5 * model_log_variance).exp(), y_0_hat

    @torch.no_grad()
    def restoration(self, y_cond, y_t=None, y_0=None, mask=None, sample_num=8, path=None, adjust=False):
        b, *_ = y_cond.shape
        assert self.num_timesteps > sample_num, 'num_timesteps must greater than sample_num'
        sample_inter = (self.num_timesteps // sample_num)
        channel_num = y_0.shape[1] if y_0 is not None else 5
        y_ts = []
        for _ in range(channel_num):
            y_ts.append(
                default(y_t, lambda: torch.randn((b, 1, y_cond.shape[2], y_cond.shape[3]), device=y_cond.device)))
        ret_arr = torch.cat(y_ts, dim=1)
        y_t = torch.cat(y_ts, dim=0)
        y_cond = y_cond.repeat((channel_num, 1, 1, 1))
        for i in tqdm(reversed(range(0, self.num_timesteps)), desc='sampling loop time step', total=self.num_timesteps):
            t = torch.full((b,), i, device=y_cond.device, dtype=torch.long)
            t = t.repeat((channel_num))
            # for c in range(channel_num):
            #     channel_index = torch.full((b,), c, device=y_cond.device, dtype=torch.long)
            #     y_t = y_ts[c]
            #     y_t, y_0_hat = self.p_sample(y_t, t, channel_index, y_cond=y_cond, path=path, adjust=adjust)
            #     y_ts[c] = y_t
            channel_index = torch.full((b,), 0, device=y_cond.device, dtype=torch.long)
            for c in range(1, channel_num):
                channel_index = torch.cat([channel_index, torch.full((b,), c, device=y_cond.device, dtype=torch.long)], dim=0)
            # print(t.shape, channel_index.shape, y_cond.shape)
            y_t, y_0_hat = self.p_sample(y_t, t, channel_index, y_cond=y_cond, path=path, adjust=adjust)
            #     y_t = y_ts[c]
            #     y_t, y_0_hat = self.p_sample(y_t, t, channel_index, y_cond=y_cond, path=path, adjust=adjust)
            #     y_ts[c] = y_t
        y_ts = y_t[:b]
        for c in range(1, channel_num):
            y_ts = torch.cat([y_ts, y_t[c * b:(c+1) * b]], dim=1)
        return y_ts, ret_arr
        # return torch.cat(y_ts, dim=1), ret_arr

    def validation(self, y_cond, y_t=None, y_0=None, mask=None, sample_num=8, path=None, adjust=False):
        b, *_ = y_cond.shape

        assert self.num_timesteps > sample_num, 'num_timesteps must greater than sample_num'
        sample_inter = (self.num_timesteps // sample_num)
        channel_num = y_0.shape[1] if y_0 is not None else 5
        c = torch.randint(0, channel_num, (1,))
        y_t = default(y_t, lambda: torch.randn((b, 1, y_cond.shape[2], y_cond.shape[3]), device=y_cond.device))
        ret_arr = y_t

        for i in tqdm(reversed(range(0, self.num_timesteps)), desc='sampling loop time step', total=self.num_timesteps):
            t = torch.full((b,), i, device=y_cond.device, dtype=torch.long)
            channel_index = torch.full((b,), c.item(), device=y_cond.device, dtype=torch.long)
            y_t, y_0_hat = self.p_sample(y_t, t, channel_index, y_cond=y_cond, path=path, adjust=adjust)
        return y_t, ret_arr, y_0[:, c.item(), :, :].unsqueeze_(dim=1)

    def forward(self, y_0, y_cond=None, mask=None, noise=None):
        # sampling from p(gammas)
        # y_0 = y_0.view((-1, y_0.shape[2],  y_0.shape[3])).unsqueeze_(dim=1)
        b, *_ = y_0.shape
        channel_index = torch.randint(0, y_0.shape[1], (b, 1, 1, 1), device=y_0.device).long()
        channel_index_repeat = channel_index.repeat((1, 1, y_0.shape[2], y_0.shape[3]))
        y_0 = y_0.gather(1, channel_index_repeat)

        t = torch.randint(1, self.num_timesteps, (b,), device=y_0.device).long()
        gamma_t1 = extract(self.gammas, t - 1, x_shape=(1, 1))
        sqrt_gamma_t2 = extract(self.gammas, t, x_shape=(1, 1))
        sample_gammas = (sqrt_gamma_t2 - gamma_t1) * torch.rand((b, 1), device=y_0.device) + gamma_t1  # Todo: why
        sample_gammas = sample_gammas.view(b, -1)

        noise = default(noise, lambda: torch.randn_like(y_0))
        y_noisy = self.q_sample(
            y_0=y_0, sample_gammas=sample_gammas.view(-1, 1, 1, 1), noise=noise)

        if mask is not None:
            noise_hat = self.denoise_fn(torch.cat([y_cond, y_noisy * mask + (1. - mask) * y_0], dim=1), sample_gammas)
            loss = self.loss_fn(mask * noise, mask * noise_hat)
        else:
            noise_hat = self.denoise_fn(torch.cat([y_cond, y_noisy], dim=1), sample_gammas, channel_index)
            loss = self.loss_fn(noise_hat, noise)
            # print(noise.shape, noise_hat.shape)
        return loss


# gaussian diffusion trainer class
def exists(x):
    return x is not None


def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d


def extract(a, t, x_shape=(1, 1, 1, 1)):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))


# beta_schedule function
def _warmup_beta(linear_start, linear_end, n_timestep, warmup_frac):
    betas = linear_end * np.ones(n_timestep, dtype=np.float64)
    warmup_time = int(n_timestep * warmup_frac)
    betas[:warmup_time] = np.linspace(
        linear_start, linear_end, warmup_time, dtype=np.float64)
    return betas


def make_beta_schedule(schedule, n_timestep, linear_start=1e-6, linear_end=1e-2, cosine_s=8e-3):
    if schedule == 'quad':
        betas = np.linspace(linear_start ** 0.5, linear_end ** 0.5,
                            n_timestep, dtype=np.float64) ** 2
    elif schedule == 'linear':
        betas = np.linspace(linear_start, linear_end,
                            n_timestep, dtype=np.float64)
    elif schedule == 'warmup10':
        betas = _warmup_beta(linear_start, linear_end,
                             n_timestep, 0.1)
    elif schedule == 'warmup50':
        betas = _warmup_beta(linear_start, linear_end,
                             n_timestep, 0.5)
    elif schedule == 'const':
        betas = linear_end * np.ones(n_timestep, dtype=np.float64)
    elif schedule == 'jsd':  # 1/T, 1/(T-1), 1/(T-2), ..., 1
        betas = 1. / np.linspace(n_timestep,
                                 1, n_timestep, dtype=np.float64)
    elif schedule == "cosine":
        timesteps = (
                torch.arange(n_timestep + 1, dtype=torch.float64) /
                n_timestep + cosine_s
        )
        alphas = timesteps / (1 + cosine_s) * math.pi / 2
        alphas = torch.cos(alphas).pow(2)
        alphas = alphas / alphas[0]
        betas = 1 - alphas[1:] / alphas[:-1]
        betas = betas.clamp(max=0.999)
    else:
        raise NotImplementedError(schedule)
    return betas
