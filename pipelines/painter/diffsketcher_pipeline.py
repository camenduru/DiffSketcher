# -*- coding: utf-8 -*-
# Copyright (c) XiMing Xing. All rights reserved.
# Author: XiMing Xing
# Description:
import pathlib
from PIL import Image
from functools import partial

import torch
import torch.nn.functional as F
from torchvision import transforms
from torchvision.datasets.folder import is_image_file
from tqdm.auto import tqdm
import numpy as np
from skimage.color import rgb2gray
import diffusers

from libs.engine import ModelState
from libs.metric.lpips_origin import LPIPS
from libs.metric.piq.perceptual import DISTS as DISTS_PIQ
from libs.metric.clip_score import CLIPScoreWrapper
from methods.painter.diffsketcher import (
    Painter, SketchPainterOptimizer, Token2AttnMixinASDSPipeline, Token2AttnMixinASDSSDXLPipeline)
from methods.painter.diffsketcher.sketch_utils import (
    log_tensor_img, plt_batch, plt_attn, save_tensor_img, fix_image_scale)
from methods.painter.diffsketcher.mask_utils import get_mask_u2net
from methods.token2attn.attn_control import AttentionStore, EmptyControl
from methods.token2attn.ptp_utils import view_images
from methods.diffusers_warp import init_diffusion_pipeline, model2res
from methods.diffvg_warp import init_diffvg
from methods.painter.diffsketcher.process_svg import remove_low_opacity_paths


class DiffSketcherPipeline(ModelState):

    def __init__(self, args):
        attn_log_ = ""
        if args.attention_init:
            attn_log_ = f"-tk{args.token_ind}" \
                        f"{'-XDoG' if args.xdog_intersec else ''}" \
                        f"-atc{args.attn_coeff}-tau{args.softmax_temp}"
        logdir_ = f"sd{args.seed}-im{args.image_size}" \
                  f"-P{args.num_paths}W{args.width}{'OP' if args.optim_opacity else 'BL'}" \
                  f"{attn_log_}"
        super().__init__(args, log_path_suffix=logdir_)

        # create log dir
        self.png_logs_dir = self.results_path / "png_logs"
        self.svg_logs_dir = self.results_path / "svg_logs"
        self.attn_logs_dir = self.results_path / "attn_logs"
        if self.accelerator.is_main_process:
            self.png_logs_dir.mkdir(parents=True, exist_ok=True)
            self.svg_logs_dir.mkdir(parents=True, exist_ok=True)
            self.attn_logs_dir.mkdir(parents=True, exist_ok=True)

        init_diffvg(self.device, True, args.print_timing)

        if args.model_id == "sdxl":
            # default LSDSSDXLPipeline scheduler is EulerDiscreteScheduler
            # when LSDSSDXLPipeline calls, scheduler.timesteps will change in step 4
            # which causes problem in sds add_noise() function
            # because the random t may not in scheduler.timesteps
            custom_pipeline = Token2AttnMixinASDSSDXLPipeline
            custom_scheduler = diffusers.DPMSolverMultistepScheduler
            self.args.cross_attn_res = self.args.cross_attn_res * 2
        elif args.model_id == 'sd21':
            custom_pipeline = Token2AttnMixinASDSPipeline
            custom_scheduler = diffusers.DDIMScheduler
        else:  # sd14, sd15
            custom_pipeline = Token2AttnMixinASDSPipeline
            custom_scheduler = diffusers.DDIMScheduler

        self.diffusion = init_diffusion_pipeline(
            self.args.model_id,
            custom_pipeline=custom_pipeline,
            custom_scheduler=custom_scheduler,
            device=self.device,
            local_files_only=not args.download,
            force_download=args.force_download,
            resume_download=args.resume_download,
            ldm_speed_up=args.ldm_speed_up,
            enable_xformers=args.enable_xformers,
            gradient_checkpoint=args.gradient_checkpoint,
        )

        self.g_device = torch.Generator(device=self.device).manual_seed(args.seed)

        # init clip model and clip score wrapper
        self.cargs = self.args.clip
        self.clip_score_fn = CLIPScoreWrapper(self.cargs.model_name,
                                              device=self.device,
                                              visual_score=True,
                                              feats_loss_type=self.cargs.feats_loss_type,
                                              feats_loss_weights=self.cargs.feats_loss_weights,
                                              fc_loss_weight=self.cargs.fc_loss_weight)
        self.opacity_delta = self.args.opacity_delta

    def load_render(self, target_img, attention_map, mask=None):
        renderer = Painter(self.args,
                           num_strokes=self.args.num_paths,
                           num_segments=self.args.num_segments,
                           imsize=self.args.image_size,
                           device=self.device,
                           target_im=target_img,
                           attention_map=attention_map,
                           mask=mask)
        return renderer

    def extract_ldm_attn(self, prompts):
        # init controller
        controller = AttentionStore() if self.args.attention_init else EmptyControl()

        height = width = model2res(self.args.model_id)
        outputs = self.diffusion(prompt=[prompts],
                                 negative_prompt=self.args.negative_prompt,
                                 height=height,
                                 width=width,
                                 controller=controller,
                                 num_inference_steps=self.args.num_inference_steps,
                                 guidance_scale=self.args.guidance_scale,
                                 generator=self.g_device)

        target_file = self.results_path / "ldm_generated_image.png"
        view_images([np.array(img) for img in outputs.images], save_image=True, fp=target_file)

        if self.args.attention_init:
            """ldm cross-attention map"""
            cross_attention_maps, tokens = \
                self.diffusion.get_cross_attention([prompts],
                                                   controller,
                                                   res=self.args.cross_attn_res,
                                                   from_where=("up", "down"),
                                                   save_path=self.results_path / "cross_attn.png")

            self.print(f"the length of tokens is {len(tokens)}, select {self.args.token_ind}-th token")
            # [res, res, seq_len]
            self.print(f"origin cross_attn_map shape: {cross_attention_maps.shape}")
            # [res, res]
            cross_attn_map = cross_attention_maps[:, :, self.args.token_ind]
            self.print(f"select cross_attn_map shape: {cross_attn_map.shape}\n")
            cross_attn_map = 255 * cross_attn_map / cross_attn_map.max()
            # [res, res, 3]
            cross_attn_map = cross_attn_map.unsqueeze(-1).expand(*cross_attn_map.shape, 3)
            # [3, res, res]
            cross_attn_map = cross_attn_map.permute(2, 0, 1).unsqueeze(0)
            # [3, clip_size, clip_size]
            cross_attn_map = F.interpolate(cross_attn_map, size=self.args.image_size, mode='bicubic')
            cross_attn_map = torch.clamp(cross_attn_map, min=0, max=255)
            # rgb to gray
            cross_attn_map = rgb2gray(cross_attn_map.squeeze(0).permute(1, 2, 0)).astype(np.float32)
            # torch to numpy
            if cross_attn_map.shape[-1] != self.args.image_size and cross_attn_map.shape[-2] != self.args.image_size:
                cross_attn_map = cross_attn_map.reshape(self.args.image_size, self.args.image_size)
            # to [0, 1]
            cross_attn_map = (cross_attn_map - cross_attn_map.min()) / (cross_attn_map.max() - cross_attn_map.min())
            """ldm self-attention map"""
            self_attention_maps, svd, vh_ = \
                self.diffusion.get_self_attention_comp([prompts],
                                                       controller,
                                                       res=self.args.self_attn_res,
                                                       from_where=("up", "down"),
                                                       img_size=self.args.image_size,
                                                       max_com=self.args.max_com,
                                                       save_path=self.results_path)

            # comp self-attention map
            if self.args.mean_comp:
                self_attn = np.mean(vh_, axis=0)
                self.print(f"use the mean of {self.args.max_com} comps.")
            else:
                self_attn = vh_[self.args.comp_idx]
                self.print(f"select {self.args.comp_idx}-th comp.")
            # to [0, 1]
            self_attn = (self_attn - self_attn.min()) / (self_attn.max() - self_attn.min())
            # visual final self-attention
            self_attn_vis = np.copy(self_attn)
            self_attn_vis = self_attn_vis * 255
            self_attn_vis = np.repeat(np.expand_dims(self_attn_vis, axis=2), 3, axis=2).astype(np.uint8)
            view_images(self_attn_vis, save_image=True, fp=self.results_path / "self-attn-final.png")

            """attention map fusion"""
            attn_map = self.args.attn_coeff * cross_attn_map + (1 - self.args.attn_coeff) * self_attn
            # to [0, 1]
            attn_map = (attn_map - attn_map.min()) / (attn_map.max() - attn_map.min())

            self.print(f"-> fusion attn_map: {attn_map.shape}")
        else:
            attn_map = None

        return target_file.as_posix(), attn_map

    @property
    def clip_norm_(self):
        return transforms.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))

    def clip_pair_augment(self,
                          x: torch.Tensor,
                          y: torch.Tensor,
                          im_res: int,
                          augments: str = "affine_norm",
                          num_aug: int = 4):
        # init augmentations
        augment_list = []
        if "affine" in augments:
            augment_list.append(
                transforms.RandomPerspective(fill=0, p=1.0, distortion_scale=0.5)
            )
            augment_list.append(
                transforms.RandomResizedCrop(im_res, scale=(0.8, 0.8), ratio=(1.0, 1.0))
            )
        augment_list.append(self.clip_norm_)  # CLIP Normalize

        # compose augmentations
        augment_compose = transforms.Compose(augment_list)
        # make augmentation pairs
        x_augs, y_augs = [self.clip_score_fn.normalize(x)], [self.clip_score_fn.normalize(y)]
        # repeat N times
        for n in range(num_aug):
            augmented_pair = augment_compose(torch.cat([x, y]))
            x_augs.append(augmented_pair[0].unsqueeze(0))
            y_augs.append(augmented_pair[1].unsqueeze(0))
        xs = torch.cat(x_augs, dim=0)
        ys = torch.cat(y_augs, dim=0)
        return xs, ys

    def painterly_rendering(self, prompt):
        # log prompts
        self.print(f"prompt: {prompt}")
        self.print(f"negative_prompt: {self.args.negative_prompt}\n")

        # init attention
        target_file, attention_map = self.extract_ldm_attn(prompt)

        timesteps_ = self.diffusion.scheduler.timesteps.cpu().numpy().tolist()
        self.print(f"{len(timesteps_)} denoising steps, {timesteps_}")

        perceptual_loss_fn = None
        if self.args.perceptual.coeff > 0:
            if self.args.perceptual.name == "lpips":
                lpips_loss_fn = LPIPS(net=self.args.perceptual.lpips_net).to(self.device)
                perceptual_loss_fn = partial(lpips_loss_fn.forward, return_per_layer=False, normalize=False)
            elif self.args.perceptual.name == "dists":
                perceptual_loss_fn = DISTS_PIQ()

        inputs, mask = self.get_target(target_file,
                                       self.args.image_size,
                                       self.results_path,
                                       self.args.u2net_path,
                                       self.args.mask_object,
                                       self.args.fix_scale,
                                       self.device)
        inputs = inputs.detach()  # inputs as GT
        self.print("inputs shape: ", inputs.shape)

        # load renderer
        renderer = Painter(self.args,
                           num_strokes=self.args.num_paths,
                           num_segments=self.args.num_segments,
                           imsize=self.args.image_size,
                           device=self.device,
                           target_im=inputs,
                           attention_map=attention_map,
                           mask=mask)

        # init img
        img = renderer.init_image(stage=0)
        self.print("init_image shape: ", img.shape)
        log_tensor_img(img, self.results_path, output_prefix="init_sketch")
        # load optimizer
        optimizer = SketchPainterOptimizer(renderer,
                                           self.args.lr,
                                           self.args.optim_opacity,
                                           self.args.optim_rgba,
                                           self.args.color_lr,
                                           self.args.optim_width,
                                           self.args.width_lr)
        optimizer.init_optimizers()

        # log params
        self.print(f"-> Painter points Params: {len(renderer.get_points_params())}")
        self.print(f"-> Painter width Params: {len(renderer.get_width_parameters())}")
        self.print(f"-> Painter opacity Params: {len(renderer.get_color_parameters())}")

        best_visual_loss, best_semantic_loss = 100, 100
        best_iter_v, best_iter_s = 0, 0
        min_delta = 1e-6

        self.print(f"\ntotal optimization steps: {self.args.num_iter}")
        with tqdm(initial=self.step, total=self.args.num_iter, disable=not self.accelerator.is_main_process) as pbar:
            while self.step < self.args.num_iter:
                raster_sketch = renderer.get_image().to(self.device)

                # ASDS loss
                sds_loss, grad = torch.tensor(0), torch.tensor(0)
                if self.step >= self.args.sds.warmup:
                    grad_scale = self.args.sds.grad_scale if self.step > self.args.sds.warmup else 0
                    sds_loss, grad = self.diffusion.score_distillation_sampling(
                        raster_sketch,
                        crop_size=self.args.sds.crop_size,
                        augments=self.args.sds.augmentations,
                        prompt=[self.args.prompt],
                        negative_prompt=self.args.negative_prompt,
                        guidance_scale=self.args.sds.guidance_scale,
                        grad_scale=grad_scale,
                        t_range=list(self.args.sds.t_range),
                    )

                # CLIP data augmentation
                raster_sketch_aug, inputs_aug = self.clip_pair_augment(
                    raster_sketch, inputs,
                    im_res=224,
                    augments=self.cargs.augmentations,
                    num_aug=self.cargs.num_aug
                )

                # clip visual loss
                total_visual_loss = torch.tensor(0)
                l_clip_fc, l_clip_conv, clip_conv_loss_sum = torch.tensor(0), [], torch.tensor(0)
                if self.args.clip.vis_loss > 0:
                    l_clip_fc, l_clip_conv = self.clip_score_fn.compute_visual_distance(
                        raster_sketch_aug, inputs_aug, clip_norm=False
                    )
                    clip_conv_loss_sum = sum(l_clip_conv)
                    total_visual_loss = self.args.clip.vis_loss * (clip_conv_loss_sum + l_clip_fc)

                # text-visual loss
                l_tvd = torch.tensor(0.)
                if self.cargs.text_visual_coeff > 0:
                    l_tvd = self.clip_score_fn.compute_text_visual_distance(
                        raster_sketch_aug, self.args.prompt
                    ) * self.cargs.text_visual_coeff

                # perceptual loss
                l_percep = torch.tensor(0.)
                if perceptual_loss_fn is not None:
                    l_perceptual = perceptual_loss_fn(raster_sketch, inputs).mean()
                    l_percep = l_perceptual * self.args.perceptual.coeff

                # total loss
                loss = sds_loss + total_visual_loss + l_tvd + l_percep

                # optimization
                optimizer.zero_grad_()
                loss.backward()
                optimizer.step_()

                # update lr
                if self.args.lr_scheduler:
                    optimizer.update_lr(self.step, self.args.decay_steps)

                # records
                pbar.set_description(
                    f"lr: {optimizer.get_lr():.2f}, "
                    f"l_total: {loss.item():.4f}, "
                    f"l_clip_fc: {l_clip_fc.item():.4f}, "
                    f"l_clip_conv({len(l_clip_conv)}): {clip_conv_loss_sum.item():.4f}, "
                    f"l_tvd: {l_tvd.item():.4f}, "
                    f"l_percep: {l_percep.item():.4f}, "
                    f"sds: {grad.item():.4e}"
                )

                # log raster and svg
                if self.step % self.args.save_step == 0 and self.accelerator.is_main_process:
                    # log png
                    plt_batch(inputs,
                              raster_sketch,
                              self.step,
                              prompt,
                              save_path=self.png_logs_dir.as_posix(),
                              name=f"iter{self.step}")
                    # log svg
                    renderer.save_svg(self.svg_logs_dir.as_posix(), f"svg_iter{self.step}")
                    if self.step >= self.args.sds.warmup and self.step == self.args.num_iter - 10:
                        remove_low_opacity_paths(f'{self.svg_logs_dir.as_posix()}/{f"svg_iter{self.step}"}.svg',
                                                 f'{self.svg_logs_dir.as_posix()}/{f"svg_iter{self.step}"}.svg',
                                                 self.opacity_delta)

                    # log cross attn
                    if self.args.log_cross_attn:
                        controller = AttentionStore()
                        _, _ = self.diffusion.get_cross_attention([self.args.prompt],
                                                                  controller,
                                                                  res=self.args.cross_attn_res,
                                                                  from_where=("up", "down"),
                                                                  save_path=self.attn_logs_dir / f"iter{self.step}.png")

                # logging the best raster images and SVG
                if self.step % self.args.eval_step == 0 and self.accelerator.is_main_process:
                    with torch.no_grad():
                        # visual metric
                        l_clip_fc, l_clip_conv = self.clip_score_fn.compute_visual_distance(
                            raster_sketch_aug, inputs_aug, clip_norm=False
                        )
                        loss_eval = sum(l_clip_conv) + l_clip_fc

                        cur_delta = loss_eval.item() - best_visual_loss
                        if abs(cur_delta) > min_delta and cur_delta < 0:
                            best_visual_loss = loss_eval.item()
                            best_iter_v = self.step
                            plt_batch(inputs,
                                      raster_sketch,
                                      best_iter_v,
                                      prompt,
                                      save_path=self.results_path.as_posix(),
                                      name="visual_best")
                            renderer.save_svg(self.results_path.as_posix(), "visual_best")
                        # semantic metric
                        loss_eval = self.clip_score_fn.compute_text_visual_distance(
                            raster_sketch_aug, self.args.prompt
                        )
                        cur_delta = loss_eval.item() - best_semantic_loss
                        if abs(cur_delta) > min_delta and cur_delta < 0:
                            best_semantic_loss = loss_eval.item()
                            best_iter_s = self.step
                            plt_batch(inputs,
                                      raster_sketch,
                                      best_iter_s,
                                      prompt,
                                      save_path=self.results_path.as_posix(),
                                      name="semantic_best")
                            renderer.save_svg(self.results_path.as_posix(), "semantic_best")

                # log attention
                if self.step == 0 and self.args.attention_init and self.accelerator.is_main_process:
                    plt_attn(renderer.get_attn(),
                             renderer.get_thresh(),
                             inputs,
                             renderer.get_inds(),
                             (self.results_path / "attention_map.jpg").as_posix())

                self.step += 1
                pbar.update(1)

        # saving final result
        renderer.save_svg(self.svg_logs_dir.as_posix(), "final_svg_tmp")
        remove_low_opacity_paths(self.svg_logs_dir / "final_svg_tmp.svg",
                                 self.results_path / "final_svg.svg",
                                 self.opacity_delta)

        final_raster_sketch = renderer.get_image().to(self.device)
        save_tensor_img(final_raster_sketch,
                        save_path=self.results_path,
                        name='final_render')

        self.close(msg="painterly rendering complete.")

    def get_target(self,
                   target_file,
                   image_size,
                   output_dir,
                   u2net_path,
                   mask_object,
                   fix_scale,
                   device):
        if not is_image_file(target_file):
            raise TypeError(f"{target_file} is not image file.")

        target = Image.open(target_file)

        if target.mode == "RGBA":
            # Create a white rgba background
            new_image = Image.new("RGBA", target.size, "WHITE")
            # Paste the image on the background.
            new_image.paste(target, (0, 0), target)
            target = new_image
        target = target.convert("RGB")

        # U2Net mask
        mask = target
        if mask_object:
            if pathlib.Path(u2net_path).exists():
                masked_im, mask = get_mask_u2net(target, output_dir, u2net_path, device)
                target = masked_im
            else:
                self.print(f"'{u2net_path}' is not exist, disable mask target")

        if fix_scale:
            target = fix_image_scale(target)

        # define image transforms
        transforms_ = []
        if target.size[0] != target.size[1]:
            transforms_.append(transforms.Resize((image_size, image_size)))
        else:
            transforms_.append(transforms.Resize(image_size))
            transforms_.append(transforms.CenterCrop(image_size))
        transforms_.append(transforms.ToTensor())

        # preprocess
        data_transforms = transforms.Compose(transforms_)
        target_ = data_transforms(target).unsqueeze(0).to(self.device)

        return target_, mask
