# Copyright 2021 Alex Yu

# First, install svox2
# Then, python opt.py <path_to>/nerf_synthetic/<scene> -t ckpt/<some_name>
# or use launching script:   sh launch.sh <EXP_NAME> <GPU> <DATA_DIR>
import torch
import torch.cuda
import torch.nn.functional as F
import svox2
import json
import imageio
import os
from os import path
import shutil
import gc
import numpy as np
import math
import argparse
from util.dataset import Dataset
from util.util import Timing, get_expon_lr_func

from torch.utils.tensorboard import SummaryWriter

from tqdm import tqdm
from typing import NamedTuple, Optional, Union

device = "cuda" if torch.cuda.is_available() else "cpu"

parser = argparse.ArgumentParser()
parser.add_argument('data_dir', type=str)
group = parser.add_argument_group("general")
group.add_argument('--train_dir', '-t', type=str, default='ckpt',
                     help='checkpoint and logging directory')
group.add_argument('--final_reso', type=int, default=512,
                   help='FINAL grid resolution')
group.add_argument('--init_reso', type=int, default=256,
                   help='INITIAL grid resolution')
group.add_argument('--ref_reso', type=int, default=256,
                   help='reference grid resolution (for adjusting lr)')
group.add_argument('--sh_dim', type=int, default=9, help='SH dimensions, must be square number >=1, <= 16')
group.add_argument('--scene_scale', type=float, default=5/6,
                           help='Scene scale; generally 2/3, can be 5/6 for lego')

group = parser.add_argument_group("optimization")
group.add_argument('--batch_size', type=int, default=5000, help='batch size')


group.add_argument('--lr_sigma', type=float, default=5e1, #5e1,#2e0,#1e8
        help='SGD lr for sigma')
group.add_argument('--lr_sigma_final', type=float, default=5e-1)
group.add_argument('--lr_sigma_decay_steps', type=int, default=250000)
group.add_argument('--lr_sigma_delay_steps', type=int, default=20000)
group.add_argument('--lr_sigma_delay_mult', type=float, default=1e-2)


group.add_argument('--lr_sh', type=float, default=2e6, help='SGD lr for SH')
group.add_argument('--lr_sh_final', type=float, default=2e5)
group.add_argument('--lr_sh_decay_steps', type=int, default=250000)
group.add_argument('--lr_sh_delay_steps', type=int, default=20000)
group.add_argument('--lr_sh_delay_mult', type=float, default=1e-2)
group.add_argument('--lr_sh_upscale_factor', type=float, default=2)

group.add_argument('--n_epochs', type=int, default=55)
group.add_argument('--print_every', type=int, default=20, help='print every')
group.add_argument('--upsamp_every', type=int, default=3,#4,
                    help='upsample the grid every')

group = parser.add_argument_group("initialization")
group.add_argument('--init_rgb', type=float, default=0.0, help='initialization rgb (pre-sigmoid)')
group.add_argument('--init_sigma', type=float, default=0.1, help='initialization sigma')


group = parser.add_argument_group("misc experiments")
group.add_argument('--perm', action='store_true', default=True,
                    help='sample by permutation of rays (true epoch) instead of '
                         'uniformly random rays')
group.add_argument('--sigma_thresh', type=float,
                    default=2.5,
                   help='Resample (upsample to 512) sigma threshold')
group.add_argument('--weight_thresh', type=float,
                    default=0.001,
                   help='Resample (upsample to 512) weight threshold')
group.add_argument('--use_weight_thresh', action='store_true', default=True,
                    help='use weight thresholding')
group.add_argument('--prox_l1_alpha', type=float, default=0.0,
                   help='proximal L1 per epoch; amount to subtract from sigma')
group.add_argument('--prox_l0', action='store_true', default=False,
                   help='proximal L0 i.e., keep resampling after each epoch')
group.add_argument('--norand', action='store_true', default=True,
                   help='disable random')

group.add_argument('--tune_mode', action='store_true', default=False,
                   help='hypertuning mode (do not eval or save until the end)')
group.add_argument('--no_save', action='store_true', default=False,
                   help='do not save at all')

group.add_argument('--rms_beta', type=float, default=0.9)
group.add_argument('--lambda_tv', type=float, default=0.0)
group.add_argument('--aniso_tv', action='store_true', default=False)
group.add_argument('--weight_decay_sigma', type=float, default=1.0)
group.add_argument('--weight_decay_sh', type=float, default=1.0)

group.add_argument('--lr_decay', action='store_true', default=True)
args = parser.parse_args()

os.makedirs(args.train_dir, exist_ok=True)
summary_writer = SummaryWriter(args.train_dir)

with open(path.join(args.train_dir, 'args.json'), 'w') as f:
    json.dump(args.__dict__, f, indent=2)
    shutil.copyfile(__file__, path.join(args.train_dir, 'opt.py'))

torch.manual_seed(20200823)
np.random.seed(20200823)

reso = args.init_reso
factor = args.ref_reso // reso

dset = Dataset(args.data_dir, split="train", device=device, permutation=args.perm,
               factor=factor,
               scene_scale=args.scene_scale)
dset.shuffle_rays()
dset_test = Dataset(args.data_dir, split="test", scene_scale=args.scene_scale)

grid = svox2.SparseGrid(reso=reso,
                        radius=1.0,
                        basis_dim=args.sh_dim,
                        use_z_order=True,
                        device=device)
grid.data.data[..., 1:] = args.init_rgb
grid.data.data[..., :1] = args.init_sigma

grid.requires_grad_(True)
step_size = 0.5  # 0.5 of a voxel!
#  step_size = 2.0

grid.opt.step_size = step_size
grid.opt.sigma_thresh = 1e-8
grid.opt.backend = 'cuvol'

gstep_id_base = 0

resample_cameras = [
        svox2.Camera(c2w.to(device=device), dset.focal, dset.focal,
                     dset.w, dset.h) for c2w in dset.c2w
    ] if args.use_weight_thresh else None
ckpt_path = path.join(args.train_dir, 'ckpt.npz')

rms : torch.Tensor = torch.zeros_like(grid.data[..., :1])

lr_sigma_func = get_expon_lr_func(args.lr_sigma, args.lr_sigma_final, args.lr_sigma_delay_steps,
                                  args.lr_sigma_delay_mult, args.lr_sigma_decay_steps)
lr_sh_func = get_expon_lr_func(args.lr_sh, args.lr_sh_final, args.lr_sh_delay_steps,
                               args.lr_sh_delay_mult, args.lr_sh_decay_steps)
lr_sigma_factor = 1.0
lr_sh_factor = 1.0

for epoch_id in range(args.n_epochs):
    epoch_size = dset.rays.origins.size(0)
    batches_per_epoch = (epoch_size-1)//args.batch_size+1
    # Test
    def eval_step():
        # Put in a function to avoid memory leak
        print('Eval step')
        with torch.no_grad():
            stats_test = {'psnr' : 0.0, 'mse' : 0.0}
            N_IMGS_TO_SAVE = 5
            N_IMGS_TO_EVAL = 20 if epoch_id > 0 else 5
            img_eval_interval = dset_test.n_images // N_IMGS_TO_EVAL
            img_save_interval = img_eval_interval * (N_IMGS_TO_EVAL // N_IMGS_TO_SAVE)
            n_images_gen = 0
            for img_id in tqdm(range(0, dset_test.n_images, img_eval_interval)):
                c2w = dset_test.c2w[img_id].to(device=device)
                cam = svox2.Camera(c2w, dset_test.focal, dset_test.focal,
                                   dset_test.w, dset_test.h)
                rgb_pred_test = grid.volume_render_image(cam, use_kernel=True)
                rgb_gt_test = dset_test.gt[img_id].to(device=device)
                all_mses = ((rgb_gt_test - rgb_pred_test) ** 2).cpu()
                if img_id % img_save_interval == 0:
                    summary_writer.add_image(f'test/image_{img_id:04d}',
                            rgb_pred_test.cpu(), global_step=gstep_id_base, dataformats='HWC')
                rgb_pred_test = rgb_gt_test = None
                mse_num : float = all_mses.mean().item()
                psnr = -10.0 * math.log10(mse_num)
                stats_test['mse'] += mse_num
                stats_test['psnr'] += psnr
                n_images_gen += 1

            stats_test['mse'] /= n_images_gen
            stats_test['psnr'] /= n_images_gen
            for stat_name in stats_test:
                summary_writer.add_scalar('test/' + stat_name,
                        stats_test[stat_name], global_step=gstep_id_base)
            summary_writer.add_scalar('epoch_id', float(epoch_id), global_step=gstep_id_base)
            print('eval stats:', stats_test)
    if epoch_id % factor == 0 and not args.tune_mode and not args.no_save:
        eval_step()
        gc.collect()

    def train_step():
        global rms
        print('Train step')
        pbar = tqdm(enumerate(range(0, epoch_size, args.batch_size)), total=batches_per_epoch)
        stats = {"mse" : 0.0, "psnr" : 0.0, "invsqr_mse" : 0.0}
        for iter_id, batch_begin in pbar:
            gstep_id = iter_id + gstep_id_base
            lr_sigma = lr_sigma_func(gstep_id) * lr_sigma_factor
            lr_sh = lr_sh_func(gstep_id) * lr_sh_factor
            if not args.lr_decay:
                lr_sigma = args.lr_sigma * lr_sigma_factor
                lr_sh = args.lr_sh * lr_sh_factor

            batch_end = min(batch_begin + args.batch_size, epoch_size)
            batch_origins = dset.rays.origins[batch_begin: batch_end]
            batch_dirs = dset.rays.dirs[batch_begin: batch_end]
            rgb_gt = dset.rays.gt[batch_begin: batch_end]
            rays = svox2.Rays(batch_origins, batch_dirs)
            rgb_pred = grid.volume_render(rays, use_kernel=True, randomize=not args.norand)

            mse = F.mse_loss(rgb_gt, rgb_pred)

            # Stats
            mse_num : float = mse.detach().item()
            psnr = -10.0 * math.log10(mse_num)
            stats['mse'] += mse_num
            stats['psnr'] += psnr
            stats['invsqr_mse'] += 1.0 / mse_num ** 2

            if (iter_id + 1) % args.print_every == 0:
                # Print averaged stats
                pbar.set_description(f'epoch {epoch_id}/{args.n_epochs} psnr={psnr:.2f}')
                for stat_name in stats:
                    stat_val = stats[stat_name] / args.print_every
                    summary_writer.add_scalar(stat_name, stat_val, global_step=gstep_id)
                    stats[stat_name] = 0.0
                if args.lambda_tv > 0.0:
                    with torch.no_grad():
                        tv = grid.tv()
                    # Apply TV
                    grid.inplace_tv_grad(grid.data.data,
                                scaling=-args.lambda_tv,
                                anisotropic=args.aniso_tv)
                    summary_writer.add_scalar("loss_tv", tv, global_step=gstep_id)
                summary_writer.add_scalar("lr_sh", lr_sh, global_step=gstep_id)
                summary_writer.add_scalar("lr_sigma", lr_sigma, global_step=gstep_id)

            # Backprop
            mse.backward()

            # Manual SGD step
            tmp = grid.data.grad[..., :1].clone()
            tmp.square_()
            tmp *= (1.0 - args.rms_beta)
            rms[tmp != 0.0] *= args.rms_beta
            rms += tmp
            del tmp

            grid.data.grad[..., 1:] *= args.lr_sh
            grid.data.grad[..., :1] /= (torch.sqrt(rms) + 1e-8)
            grid.data.grad[..., :1] *= lr_sigma
            grid.data.data -= grid.data.grad

            if args.weight_decay_sh < 1.0:
                grid.data.data[..., 1:] *= args.weight_decay_sh

            if args.weight_decay_sigma < 1.0:
                grid.data.data[..., :1] *= args.weight_decay_sigma

            del grid.data.grad  # Save memory


    train_step()
    gc.collect()
    gstep_id_base += batches_per_epoch

    #  ckpt_path = path.join(args.train_dir, f'ckpt_{epoch_id:05d}.npz')
    # Overwrite prev checkpoints since they are very huge
    if epoch_id % factor == 0 and not args.tune_mode:
        print('Saving', ckpt_path)
        grid.save(ckpt_path)

    if (epoch_id + 1) % args.upsamp_every == 0:
        if reso < args.final_reso or args.prox_l0:
            print('* Upsampling from', reso, 'to', reso * 2)
            non_final = reso < args.final_reso
            if non_final:
                reso *= 2
            use_sparsify = True # reso >= args.ref_reso
            grid.resample(reso=reso,
                    sigma_thresh=args.sigma_thresh if use_sparsify else 0.0,
                    weight_thresh=args.weight_thresh if use_sparsify else 0.0,
                    dilate=1, #use_sparsify,
                    cameras=resample_cameras)
            del rms
            rms : torch.Tensor = torch.zeros_like(grid.data[..., :1])
            if non_final:
                #  if reso <= args.ref_reso:
                #  lr_sigma_factor *= 8
                #  else:
                #  lr_sigma_factor *= 4
                lr_sh_factor *= args.lr_sh_upscale_factor
            print('Increased lr to (sigma:)', args.lr_sigma, '(sh:)', args.lr_sh)

        if factor > 1 and reso < args.final_reso:
            factor //= 2
            dset.gen_rays(factor=factor)
            dset.shuffle_rays()

    if args.prox_l1_alpha > 0.0:
        print('ProxL1: sigma -=', args.prox_l1_alpha)
        grid.data.data[..., :1] -= args.prox_l1_alpha

    if epoch_id == args.n_epochs - 1:
        print('Final eval and save')
        eval_step()
        if not args.no_save:
            grid.save(ckpt_path)
