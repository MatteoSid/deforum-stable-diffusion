import torch
from PIL import Image
import requests
import numpy as np
import torchvision.transforms.functional as TF
from pytorch_lightning import seed_everything
import os
from ldm.models.diffusion.plms import PLMSSampler
from ldm.models.diffusion.ddim import DDIMSampler
from k_diffusion.external import CompVisDenoiser
from torch import autocast
from contextlib import nullcontext
from einops import rearrange, repeat

from .prompt import get_uc_and_c
from .k_samplers import sampler_fn, make_inject_timing_fn
from scipy.ndimage import gaussian_filter

from .callback import SamplerCallback

from .conditioning import exposure_loss, make_mse_loss, get_color_palette, make_clip_loss_fn
from .conditioning import make_rgb_color_match_loss, blue_loss_fn, threshold_by, make_aesthetics_loss_fn, mean_loss_fn, var_loss_fn, exposure_loss
from .model_wrap import CFGDenoiserWithGrad

def add_noise(sample: torch.Tensor, noise_amt: float) -> torch.Tensor:
    return sample + torch.randn(sample.shape, device=sample.device) * noise_amt

def load_img(path, shape=None, use_alpha_as_mask=False):
    # use_alpha_as_mask: Read the alpha channel of the image as the mask image
    if path.startswith('http://') or path.startswith('https://'):
        image = Image.open(requests.get(path, stream=True).raw)
    else:
        image = Image.open(path)

    if use_alpha_as_mask:
        image = image.convert('RGBA')
    else:
        image = image.convert('RGB')

    if shape is not None:
        image = image.resize(shape, resample=Image.LANCZOS)

    mask_image = None
    if use_alpha_as_mask:
        # Split alpha channel into a mask_image
        red, green, blue, alpha = Image.Image.split(image)
        mask_image = alpha.convert('L')
        image = image.convert('RGB')

    image = np.array(image).astype(np.float16) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    image = 2.*image - 1.

    return image, mask_image

def load_mask_latent(mask_input, shape):
    # mask_input (str or PIL Image.Image): Path to the mask image or a PIL Image object
    # shape (list-like len(4)): shape of the image to match, usually latent_image.shape
    
    if isinstance(mask_input, str): # mask input is probably a file name
        if mask_input.startswith('http://') or mask_input.startswith('https://'):
            mask_image = Image.open(requests.get(mask_input, stream=True).raw).convert('RGBA')
        else:
            mask_image = Image.open(mask_input).convert('RGBA')
    elif isinstance(mask_input, Image.Image):
        mask_image = mask_input
    else:
        raise Exception("mask_input must be a PIL image or a file name")

    mask_w_h = (shape[-1], shape[-2])
    mask = mask_image.resize(mask_w_h, resample=Image.LANCZOS)
    mask = mask.convert("L")
    return mask

def prepare_mask(mask_input, mask_shape, mask_brightness_adjust=1.0, mask_contrast_adjust=1.0, invert_mask=False):
    # mask_input (str or PIL Image.Image): Path to the mask image or a PIL Image object
    # shape (list-like len(4)): shape of the image to match, usually latent_image.shape
    # mask_brightness_adjust (non-negative float): amount to adjust brightness of the iamge, 
    #     0 is black, 1 is no adjustment, >1 is brighter
    # mask_contrast_adjust (non-negative float): amount to adjust contrast of the image, 
    #     0 is a flat grey image, 1 is no adjustment, >1 is more contrast
    
    mask = load_mask_latent(mask_input, mask_shape)

    # Mask brightness/contrast adjustments
    if mask_brightness_adjust != 1:
        mask = TF.adjust_brightness(mask, mask_brightness_adjust)
    if mask_contrast_adjust != 1:
        mask = TF.adjust_contrast(mask, mask_contrast_adjust)

    # Mask image to array
    mask = np.array(mask).astype(np.float32) / 255.0
    mask = np.tile(mask,(4,1,1))
    mask = np.expand_dims(mask,axis=0)
    mask = torch.from_numpy(mask)

    if invert_mask:
        mask = ( (mask - 0.5) * -1) + 0.5
    
    mask = np.clip(mask,0,1)
    return mask

def generate(args, root, frame = 0, return_latent=False, return_sample=False, return_c=False):
    seed_everything(args.seed)
    os.makedirs(args.outdir, exist_ok=True)

    sampler = PLMSSampler(root.model) if args.sampler == 'plms' else DDIMSampler(root.model)
    model_wrap = CompVisDenoiser(root.model)
    batch_size = args.n_samples
    prompt = args.prompt
    assert prompt is not None
    data = [batch_size * [prompt]]
    precision_scope = autocast if args.precision == "autocast" else nullcontext

    init_latent = None
    mask_image = None
    init_image = None
    if args.init_latent is not None:
        init_latent = args.init_latent
    elif args.init_sample is not None:
        with precision_scope("cuda"):
            init_latent = root.model.get_first_stage_encoding(root.model.encode_first_stage(args.init_sample))
    elif args.use_init and args.init_image != None and args.init_image != '':
        init_image, mask_image = load_img(args.init_image, 
                                          shape=(args.W, args.H),  
                                          use_alpha_as_mask=args.use_alpha_as_mask)
        init_image = init_image.to(root.device)
        init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
        with precision_scope("cuda"):
            init_latent = root.model.get_first_stage_encoding(root.model.encode_first_stage(init_image))  # move to latent space        

    if not args.use_init and args.strength > 0 and args.strength_0_no_init:
        print("\nNo init image, but strength > 0. Strength has been auto set to 0, since use_init is False.")
        print("If you want to force strength > 0 with no init, please set strength_0_no_init to False.\n")
        args.strength = 0

    # Mask functions
    if args.use_mask:
        assert args.mask_file is not None or mask_image is not None, "use_mask==True: An mask image is required for a mask. Please enter a mask_file or use an init image with an alpha channel"
        assert args.use_init, "use_mask==True: use_init is required for a mask"
        assert init_latent is not None, "use_mask==True: An latent init image is required for a mask"


        mask = prepare_mask(args.mask_file if mask_image is None else mask_image, 
                            init_latent.shape, 
                            args.mask_contrast_adjust, 
                            args.mask_brightness_adjust,
                            args.invert_mask)
        
        if (torch.all(mask == 0) or torch.all(mask == 1)) and args.use_alpha_as_mask:
            raise Warning("use_alpha_as_mask==True: Using the alpha channel from the init image as a mask, but the alpha channel is blank.")
        
        mask = mask.to(root.device)
        mask = repeat(mask, '1 ... -> b ...', b=batch_size)
    else:
        mask = None

    assert not ( (args.use_mask and args.overlay_mask) and (args.init_sample is None and init_image is None)), "Need an init image when use_mask == True and overlay_mask == True"

    # Init MSE loss image
    init_mse_image = None
    if args.init_mse_scale and args.init_mse_image != None and args.init_mse_image != '':
        init_mse_image, mask_image = load_img(args.init_mse_image,
                                          shape=(args.W, args.H),
                                          use_alpha_as_mask=args.use_alpha_as_mask)
        init_mse_image = init_mse_image.to(root.device)
        init_mse_image = repeat(init_mse_image, '1 ... -> b ...', b=batch_size)

    assert not ( args.init_mse_scale != 0 and (args.init_mse_image is None or args.init_mse_image == '') ), "Need an init image when init_mse_scale != 0"

    t_enc = int((1.0-args.strength) * args.steps)

    # Noise schedule for the k-diffusion samplers (used for masking)
    k_sigmas = model_wrap.get_sigmas(args.steps)
    args.clamp_schedule = dict(zip(k_sigmas.tolist(), np.linspace(args.clamp_start,args.clamp_stop,args.steps+1)))
    k_sigmas = k_sigmas[len(k_sigmas)-t_enc-1:]

    if args.sampler in ['plms','ddim']:
        sampler.make_schedule(ddim_num_steps=args.steps, ddim_eta=args.ddim_eta, ddim_discretize='fill', verbose=False)

    if args.colormatch_scale != 0:
        assert args.colormatch_image is not None, "If using color match loss, colormatch_image is needed"
        colormatch_image, _ = load_img(args.colormatch_image)
        colormatch_image = colormatch_image.to('cpu')
        del(_)
    else:
        colormatch_image = None

    # Loss functions
    if args.init_mse_scale != 0:
        if args.decode_method == "linear":
            mse_loss_fn = make_mse_loss(root.model.linear_decode(root.model.get_first_stage_encoding(root.model.encode_first_stage(init_mse_image.to(root.device)))))
        else:
            mse_loss_fn = make_mse_loss(init_mse_image)
    else:
        mse_loss_fn = None

    if args.colormatch_scale != 0:
        _,_ = get_color_palette(root, args.colormatch_n_colors, colormatch_image, verbose=True) # display target color palette outside the latent space
        if args.decode_method == "linear":
            grad_img_shape = (int(args.W/args.f), int(args.H/args.f))
            colormatch_image = root.model.linear_decode(root.model.get_first_stage_encoding(root.model.encode_first_stage(colormatch_image.to(root.device))))
            colormatch_image = colormatch_image.to('cpu')
        else:
            grad_img_shape = (args.W, args.H)
        color_loss_fn = make_rgb_color_match_loss(root,
                                                  colormatch_image, 
                                                  n_colors=args.colormatch_n_colors, 
                                                  img_shape=grad_img_shape,
                                                  ignore_sat_weight=args.ignore_sat_weight)
    else:
        color_loss_fn = None

    if args.clip_scale != 0:
        clip_loss_fn = make_clip_loss_fn(root, args)
    else:
        clip_loss_fn = None

    if args.aesthetics_scale != 0:
        aesthetics_loss_fn = make_aesthetics_loss_fn(root, args)
    else:
        aesthetics_loss_fn = None

    if args.exposure_scale != 0:
        exposure_loss_fn = exposure_loss(args.exposure_target)
    else:
        exposure_loss_fn = None

    loss_fns_scales = [
        [clip_loss_fn,              args.clip_scale],
        [blue_loss_fn,              args.blue_scale],
        [mean_loss_fn,              args.mean_scale],
        [exposure_loss_fn,          args.exposure_scale],
        [var_loss_fn,               args.var_scale],
        [mse_loss_fn,               args.init_mse_scale],
        [color_loss_fn,             args.colormatch_scale],
        [aesthetics_loss_fn,        args.aesthetics_scale]
    ]

    callback = SamplerCallback(args=args,
                            root=root,
                            mask=mask, 
                            init_latent=init_latent,
                            sigmas=k_sigmas,
                            sampler=sampler,
                            verbose=False).callback 

    clamp_fn = threshold_by(threshold=args.clamp_grad_threshold, threshold_type=args.grad_threshold_type, clamp_schedule=args.clamp_schedule)

    grad_inject_timing_fn = make_inject_timing_fn(args.grad_inject_timing, model_wrap, args.steps)

    cfg_model = CFGDenoiserWithGrad(model_wrap, 
                                    loss_fns_scales, 
                                    clamp_fn, 
                                    args.gradient_wrt, 
                                    args.gradient_add_to, 
                                    args.cond_uncond_sync,
                                    decode_method=args.decode_method,
                                    grad_inject_timing_fn=grad_inject_timing_fn, # option to use grad in only a few of the steps
                                    grad_consolidate_fn=None, # function to add grad to image fn(img, grad, sigma)
                                    verbose=False)

    results = []
    with torch.no_grad():
        with precision_scope("cuda"):
            with root.model.ema_scope():
                for prompts in data:
                    if isinstance(prompts, tuple):
                        prompts = list(prompts)
                    if args.prompt_weighting:
                        uc, c = get_uc_and_c(prompts, root.model, args, frame)
                    else:
                        uc = root.model.get_learned_conditioning(batch_size * [""])
                        c = root.model.get_learned_conditioning(prompts)


                    if args.scale == 1.0:
                        uc = None
                    if args.init_c != None:
                        c = args.init_c

                    if args.sampler in ["klms","dpm2","dpm2_ancestral","heun","euler","euler_ancestral"]:
                        samples = sampler_fn(
                            c=c, 
                            uc=uc, 
                            args=args, 
                            model_wrap=cfg_model, 
                            init_latent=init_latent, 
                            t_enc=t_enc, 
                            device=root.device, 
                            cb=callback,
                            verbose=False)
                    else:
                        # args.sampler == 'plms' or args.sampler == 'ddim':
                        if init_latent is not None and args.strength > 0:
                            z_enc = sampler.stochastic_encode(init_latent, torch.tensor([t_enc]*batch_size).to(root.device))
                        else:
                            z_enc = torch.randn([args.n_samples, args.C, args.H // args.f, args.W // args.f], device=root.device)
                        if args.sampler == 'ddim':
                            samples = sampler.decode(z_enc, 
                                                     c, 
                                                     t_enc, 
                                                     unconditional_guidance_scale=args.scale,
                                                     unconditional_conditioning=uc,
                                                     img_callback=callback)
                        elif args.sampler == 'plms': # no "decode" function in plms, so use "sample"
                            shape = [args.C, args.H // args.f, args.W // args.f]
                            samples, _ = sampler.sample(S=args.steps,
                                                            conditioning=c,
                                                            batch_size=args.n_samples,
                                                            shape=shape,
                                                            verbose=False,
                                                            unconditional_guidance_scale=args.scale,
                                                            unconditional_conditioning=uc,
                                                            eta=args.ddim_eta,
                                                            x_T=z_enc,
                                                            img_callback=callback)
                        else:
                            raise Exception(f"Sampler {args.sampler} not recognised.")

                    
                    if return_latent:
                        results.append(samples.clone())

                    x_samples = root.model.decode_first_stage(samples)

                    if args.use_mask and args.overlay_mask:
                        # Overlay the masked image after the image is generated
                        if args.init_sample is not None:
                            img_original = args.init_sample
                        elif init_image is not None:
                            img_original = init_image
                        else:
                            raise Exception("Cannot overlay the masked image without an init image to overlay")

                        mask_fullres = prepare_mask(args.mask_file if mask_image is None else mask_image, 
                                                    img_original.shape, 
                                                    args.mask_contrast_adjust, 
                                                    args.mask_brightness_adjust,
                                                    args.invert_mask)
                        mask_fullres = mask_fullres[:,:3,:,:]
                        mask_fullres = repeat(mask_fullres, '1 ... -> b ...', b=batch_size)

                        mask_fullres[mask_fullres < mask_fullres.max()] = 0
                        mask_fullres = gaussian_filter(mask_fullres, args.mask_overlay_blur)
                        mask_fullres = torch.Tensor(mask_fullres).to(root.device)

                        x_samples = img_original * mask_fullres + x_samples * ((mask_fullres * -1.0) + 1)


                    if return_sample:
                        results.append(x_samples.clone())

                    x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                    if return_c:
                        results.append(c.clone())

                    for x_sample in x_samples:
                        x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
                        image = Image.fromarray(x_sample.astype(np.uint8))
                        results.append(image)
    return results
