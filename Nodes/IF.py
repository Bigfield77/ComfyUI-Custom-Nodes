from comfy.model_management import throw_exception_if_processing_interrupted, xformers_enabled
import torchvision.transforms.functional as TF
from transformers import T5EncoderModel
from diffusers import DiffusionPipeline
from comfy.utils import ProgressBar
import torch
import gc


class Loader:
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"model": (["I-M", "I-L", "I-XL", "II-M", "II-L", "III"], {"default": "I-M"}),
				"device": ("STRING", {"default": ""}),
			},
		}

	CATEGORY = "Zuellni/IF"
	FUNCTION = "process"
	RETURN_NAMES = ("MODEL",)
	RETURN_TYPES = ("IF_MODEL",)

	def process(self, model, device):
		if model == "III":
			model = DiffusionPipeline.from_pretrained(
				"stabilityai/stable-diffusion-x4-upscaler",
				torch_dtype = torch.float16,
				requires_safety_checker = False,
				feature_extractor = None,
				safety_checker = None,
				watermarker = None,
			)

			if xformers_enabled():
				model.enable_xformers_memory_efficient_attention()
		else:
			model = DiffusionPipeline.from_pretrained(
				f"DeepFloyd/IF-{model}-v1.0",
				variant="fp16",
				torch_dtype = torch.float16,
				requires_safety_checker = False,
				feature_extractor = None,
				safety_checker = None,
				text_encoder = None,
				watermarker = None,
			)

		if device:
			return (model.to(device),)

		model.enable_model_cpu_offload()
		return (model,)


class Encode:
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"unload": ([False, True], {"default": True}),
				"positive": ("STRING", {"default": "", "multiline": True}),
				"negative": ("STRING", {"default": "", "multiline": True}),
			},
		}

	CATEGORY = "Zuellni/IF"
	FUNCTION = "process"
	MODEL = None
	RETURN_TYPES = ("POSITIVE", "NEGATIVE",)
	TEXT_ENCODER = None

	def process(self, unload, positive, negative):
		if not Encode.MODEL:
			Encode.TEXT_ENCODER = T5EncoderModel.from_pretrained(
				"DeepFloyd/IF-I-M-v1.0",
				subfolder="text_encoder",
				variant="8bit",
				load_in_8bit = True,
				device_map="auto",
			)

			Encode.MODEL = DiffusionPipeline.from_pretrained(
				"DeepFloyd/IF-I-M-v1.0",
				text_encoder = Encode.TEXT_ENCODER,
				requires_safety_checker = False,
				feature_extractor = None,
				safety_checker = None,
				unet = None,
				watermarker = None,
			)

		positive, negative = Encode.MODEL.encode_prompt(
			prompt = positive,
			negative_prompt = negative,
		)

		if unload:
			del Encode.MODEL, Encode.TEXT_ENCODER
			gc.collect()
			Encode.MODEL = None
			Encode.TEXT_ENCODER = None

		return (positive, negative,)


class Stage_I:
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"positive": ("POSITIVE",),
				"negative": ("NEGATIVE",),
				"model": ("IF_MODEL",),
				"width": ("INT", {"default": 64, "min": 8, "max": 128, "step": 8}),
				"height": ("INT", {"default": 64, "min": 8, "max": 128, "step": 8}),
				"batch_size": ("INT", {"default": 1, "min": 1, "max": 64}),
				"seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
				"steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
				"cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
			},
		}

	CATEGORY = "Zuellni/IF"
	FUNCTION = "process"
	RETURN_NAMES = ("IMAGES",)
	RETURN_TYPES = ("IMAGE",)

	def process(self, model, positive, negative, width, height, batch_size, seed, steps, cfg):
		progress = ProgressBar(steps)

		def callback(step, time_step, latent):
			throw_exception_if_processing_interrupted()
			progress.update_absolute(step)

		images = model(
			prompt_embeds = positive,
			negative_prompt_embeds = negative,
			width = width,
			height = height,
			generator = torch.manual_seed(seed),
			guidance_scale = cfg,
			num_images_per_prompt = batch_size,
			num_inference_steps = steps,
			callback = callback,
			output_type="pt",
		).images

		images = (images / 2 + 0.5).clamp(0, 1)
		images = images.cpu().float().permute(0, 2, 3, 1)
		return (images,)


class Stage_II:
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"positive": ("POSITIVE",),
				"negative": ("NEGATIVE",),
				"model": ("IF_MODEL",),
				"images": ("IMAGE",),
				"seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
				"steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
				"cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
			},
		}

	CATEGORY = "Zuellni/IF"
	FUNCTION = "process"
	RETURN_NAMES = ("IMAGES",)
	RETURN_TYPES = ("IMAGE",)

	def process(self, model, images, positive, negative, seed, steps, cfg):
		images = images.permute(0, 3, 1, 2)
		progress = ProgressBar(steps)
		batch_size, channels, height, width = images.shape
		max_dim = max(height, width)
		images = TF.center_crop(images, max_dim)
		model.unet.config.sample_size = max_dim * 4

		if batch_size > 1:
			positive = positive.repeat(batch_size, 1, 1)
			negative = negative.repeat(batch_size, 1, 1)

		def callback(step, time_step, latent):
			throw_exception_if_processing_interrupted()
			progress.update_absolute(step)

		images = model(
			image = images,
			prompt_embeds = positive,
			negative_prompt_embeds = negative,
			generator = torch.manual_seed(seed),
			guidance_scale = cfg,
			num_inference_steps = steps,
			callback = callback,
			output_type="pt",
		).images.cpu().float()

		images = TF.center_crop(images, (height * 4, width * 4))
		images = images.permute(0, 2, 3, 1)
		return (images,)


class Stage_III:
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"model": ("IF_MODEL",),
				"images": ("IMAGE",),
				"tile": ([False, True], {"default": False}),
				"tile_size": ("INT", {"default": 512, "min": 64, "max": 1024, "step": 64}),
				"noise": ("INT", {"default": 20, "min": 0, "max": 100}),
				"seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
				"steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
				"cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
				"positive": ("STRING", {"default": "", "multiline": True}),
				"negative": ("STRING", {"default": "", "multiline": True}),
			},
		}

	CATEGORY = "Zuellni/IF"
	FUNCTION = "process"
	RETURN_NAMES = ("IMAGES",)
	RETURN_TYPES = ("IMAGE",)

	def process(self, model, images, tile, tile_size, noise, seed, steps, cfg, positive, negative):
		images = images.permute(0, 3, 1, 2)
		progress = ProgressBar(steps)
		batch_size = images.shape[0]

		if batch_size > 1:
			positive = [positive] * batch_size
			negative = [negative] * batch_size

		if tile:
			model.vae.config.sample_size = tile_size
			model.vae.enable_tiling()

		def callback(step, time_step, latent):
			throw_exception_if_processing_interrupted()
			progress.update_absolute(step)

		images = model(
			image = images,
			prompt = positive,
			negative_prompt = negative,
			noise_level = noise,
			generator = torch.manual_seed(seed),
			guidance_scale = cfg,
			num_inference_steps = steps,
			callback = callback,
			output_type="pt",
		).images.cpu().float().permute(0, 2, 3, 1)

		return (images,)