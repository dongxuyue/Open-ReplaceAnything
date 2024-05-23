# coding=utf-8
# Copyright 2024 HuggingFace Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import gc
import importlib
import os
import tempfile
import time
import unittest

import numpy as np
import torch
import torch.nn as nn
from huggingface_hub import hf_hub_download
from huggingface_hub.repocard import RepoCard
from packaging import version
from safetensors.torch import load_file
from transformers import CLIPTextModel, CLIPTextModelWithProjection, CLIPTokenizer

from diffusers import (
    AutoencoderKL,
    AutoPipelineForImage2Image,
    AutoPipelineForText2Image,
    ControlNetModel,
    DDIMScheduler,
    DiffusionPipeline,
    EulerDiscreteScheduler,
    LCMScheduler,
    StableDiffusionPipeline,
    StableDiffusionXLAdapterPipeline,
    StableDiffusionXLControlNetPipeline,
    StableDiffusionXLPipeline,
    T2IAdapter,
    UNet2DConditionModel,
)
from diffusers.utils.import_utils import is_accelerate_available, is_peft_available
from diffusers.utils.testing_utils import (
    floats_tensor,
    load_image,
    nightly,
    numpy_cosine_similarity_distance,
    require_peft_backend,
    require_peft_version_greater,
    require_torch_gpu,
    slow,
    torch_device,
)


if is_accelerate_available():
    from accelerate.utils import release_memory

if is_peft_available():
    from peft import LoraConfig
    from peft.tuners.tuners_utils import BaseTunerLayer
    from peft.utils import get_peft_model_state_dict


def state_dicts_almost_equal(sd1, sd2):
    sd1 = dict(sorted(sd1.items()))
    sd2 = dict(sorted(sd2.items()))

    models_are_equal = True
    for ten1, ten2 in zip(sd1.values(), sd2.values()):
        if (ten1 - ten2).abs().max() > 1e-3:
            models_are_equal = False

    return models_are_equal


@require_peft_backend
class PeftLoraLoaderMixinTests:
    torch_device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline_class = None
    scheduler_cls = None
    scheduler_kwargs = None
    has_two_text_encoders = False
    unet_kwargs = None
    vae_kwargs = None

    def get_dummy_components(self, scheduler_cls=None):
        scheduler_cls = self.scheduler_cls if scheduler_cls is None else LCMScheduler
        rank = 4

        torch.manual_seed(0)
        unet = UNet2DConditionModel(**self.unet_kwargs)

        scheduler = scheduler_cls(**self.scheduler_kwargs)

        torch.manual_seed(0)
        vae = AutoencoderKL(**self.vae_kwargs)

        text_encoder = CLIPTextModel.from_pretrained("peft-internal-testing/tiny-clip-text-2")
        tokenizer = CLIPTokenizer.from_pretrained("peft-internal-testing/tiny-clip-text-2")

        if self.has_two_text_encoders:
            text_encoder_2 = CLIPTextModelWithProjection.from_pretrained("peft-internal-testing/tiny-clip-text-2")
            tokenizer_2 = CLIPTokenizer.from_pretrained("peft-internal-testing/tiny-clip-text-2")

        text_lora_config = LoraConfig(
            r=rank,
            lora_alpha=rank,
            target_modules=["q_proj", "k_proj", "v_proj", "out_proj"],
            init_lora_weights=False,
        )

        unet_lora_config = LoraConfig(
            r=rank, lora_alpha=rank, target_modules=["to_q", "to_k", "to_v", "to_out.0"], init_lora_weights=False
        )

        if self.has_two_text_encoders:
            pipeline_components = {
                "unet": unet,
                "scheduler": scheduler,
                "vae": vae,
                "text_encoder": text_encoder,
                "tokenizer": tokenizer,
                "text_encoder_2": text_encoder_2,
                "tokenizer_2": tokenizer_2,
                "image_encoder": None,
                "feature_extractor": None,
            }
        else:
            pipeline_components = {
                "unet": unet,
                "scheduler": scheduler,
                "vae": vae,
                "text_encoder": text_encoder,
                "tokenizer": tokenizer,
                "safety_checker": None,
                "feature_extractor": None,
                "image_encoder": None,
            }

        return pipeline_components, text_lora_config, unet_lora_config

    def get_dummy_inputs(self, with_generator=True):
        batch_size = 1
        sequence_length = 10
        num_channels = 4
        sizes = (32, 32)

        generator = torch.manual_seed(0)
        noise = floats_tensor((batch_size, num_channels) + sizes)
        input_ids = torch.randint(1, sequence_length, size=(batch_size, sequence_length), generator=generator)

        pipeline_inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "num_inference_steps": 2,
            "guidance_scale": 6.0,
            "output_type": "np",
        }
        if with_generator:
            pipeline_inputs.update({"generator": generator})

        return noise, input_ids, pipeline_inputs

    # copied from: https://colab.research.google.com/gist/sayakpaul/df2ef6e1ae6d8c10a49d859883b10860/scratchpad.ipynb
    def get_dummy_tokens(self):
        max_seq_length = 77

        inputs = torch.randint(2, 56, size=(1, max_seq_length), generator=torch.manual_seed(0))

        prepared_inputs = {}
        prepared_inputs["input_ids"] = inputs
        return prepared_inputs

    def check_if_lora_correctly_set(self, model) -> bool:
        """
        Checks if the LoRA layers are correctly set with peft
        """
        for module in model.modules():
            if isinstance(module, BaseTunerLayer):
                return True
        return False

    def test_simple_inference(self):
        """
        Tests a simple inference and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)

            _, _, inputs = self.get_dummy_inputs()
            output_no_lora = pipe(**inputs).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

    def test_simple_inference_with_text_lora(self):
        """
        Tests a simple inference with lora attached on the text encoder
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            output_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                not np.allclose(output_lora, output_no_lora, atol=1e-3, rtol=1e-3), "Lora should change the output"
            )

    def test_simple_inference_with_text_lora_and_scale(self):
        """
        Tests a simple inference with lora attached on the text encoder + scale argument
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            output_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                not np.allclose(output_lora, output_no_lora, atol=1e-3, rtol=1e-3), "Lora should change the output"
            )

            output_lora_scale = pipe(
                **inputs, generator=torch.manual_seed(0), cross_attention_kwargs={"scale": 0.5}
            ).images
            self.assertTrue(
                not np.allclose(output_lora, output_lora_scale, atol=1e-3, rtol=1e-3),
                "Lora + scale should change the output",
            )

            output_lora_0_scale = pipe(
                **inputs, generator=torch.manual_seed(0), cross_attention_kwargs={"scale": 0.0}
            ).images
            self.assertTrue(
                np.allclose(output_no_lora, output_lora_0_scale, atol=1e-3, rtol=1e-3),
                "Lora + 0 scale should lead to same result as no LoRA",
            )

    def test_simple_inference_with_text_lora_fused(self):
        """
        Tests a simple inference with lora attached into text encoder + fuses the lora weights into base model
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.fuse_lora()
            # Fusing should still keep the LoRA layers
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            ouput_fused = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertFalse(
                np.allclose(ouput_fused, output_no_lora, atol=1e-3, rtol=1e-3), "Fused lora should change the output"
            )

    def test_simple_inference_with_text_lora_unloaded(self):
        """
        Tests a simple inference with lora attached to text encoder, then unloads the lora weights
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.unload_lora_weights()
            # unloading should remove the LoRA layers
            self.assertFalse(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly unloaded in text encoder"
            )

            if self.has_two_text_encoders:
                self.assertFalse(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2),
                    "Lora not correctly unloaded in text encoder 2",
                )

            ouput_unloaded = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                np.allclose(ouput_unloaded, output_no_lora, atol=1e-3, rtol=1e-3),
                "Fused lora should change the output",
            )

    def test_simple_inference_with_text_lora_save_load(self):
        """
        Tests a simple usecase where users could use saving utilities for LoRA.
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            images_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            with tempfile.TemporaryDirectory() as tmpdirname:
                text_encoder_state_dict = get_peft_model_state_dict(pipe.text_encoder)
                if self.has_two_text_encoders:
                    text_encoder_2_state_dict = get_peft_model_state_dict(pipe.text_encoder_2)

                    self.pipeline_class.save_lora_weights(
                        save_directory=tmpdirname,
                        text_encoder_lora_layers=text_encoder_state_dict,
                        text_encoder_2_lora_layers=text_encoder_2_state_dict,
                        safe_serialization=False,
                    )
                else:
                    self.pipeline_class.save_lora_weights(
                        save_directory=tmpdirname,
                        text_encoder_lora_layers=text_encoder_state_dict,
                        safe_serialization=False,
                    )

                self.assertTrue(os.path.isfile(os.path.join(tmpdirname, "pytorch_lora_weights.bin")))
                pipe.unload_lora_weights()

                pipe.load_lora_weights(os.path.join(tmpdirname, "pytorch_lora_weights.bin"))

            images_lora_from_pretrained = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            self.assertTrue(
                np.allclose(images_lora, images_lora_from_pretrained, atol=1e-3, rtol=1e-3),
                "Loading from saved checkpoints should give same results.",
            )

    def test_simple_inference_save_pretrained(self):
        """
        Tests a simple usecase where users could use saving utilities for LoRA through save_pretrained
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            images_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            with tempfile.TemporaryDirectory() as tmpdirname:
                pipe.save_pretrained(tmpdirname)

                pipe_from_pretrained = self.pipeline_class.from_pretrained(tmpdirname)
                pipe_from_pretrained.to(self.torch_device)

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe_from_pretrained.text_encoder),
                "Lora not correctly set in text encoder",
            )

            if self.has_two_text_encoders:
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe_from_pretrained.text_encoder_2),
                    "Lora not correctly set in text encoder 2",
                )

            images_lora_save_pretrained = pipe_from_pretrained(**inputs, generator=torch.manual_seed(0)).images

            self.assertTrue(
                np.allclose(images_lora, images_lora_save_pretrained, atol=1e-3, rtol=1e-3),
                "Loading from saved checkpoints should give same results.",
            )

    def test_simple_inference_with_text_unet_lora_save_load(self):
        """
        Tests a simple usecase where users could use saving utilities for LoRA for Unet + text encoder
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            pipe.unet.add_adapter(unet_lora_config)

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            images_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            with tempfile.TemporaryDirectory() as tmpdirname:
                text_encoder_state_dict = get_peft_model_state_dict(pipe.text_encoder)
                unet_state_dict = get_peft_model_state_dict(pipe.unet)
                if self.has_two_text_encoders:
                    text_encoder_2_state_dict = get_peft_model_state_dict(pipe.text_encoder_2)

                    self.pipeline_class.save_lora_weights(
                        save_directory=tmpdirname,
                        text_encoder_lora_layers=text_encoder_state_dict,
                        text_encoder_2_lora_layers=text_encoder_2_state_dict,
                        unet_lora_layers=unet_state_dict,
                        safe_serialization=False,
                    )
                else:
                    self.pipeline_class.save_lora_weights(
                        save_directory=tmpdirname,
                        text_encoder_lora_layers=text_encoder_state_dict,
                        unet_lora_layers=unet_state_dict,
                        safe_serialization=False,
                    )

                self.assertTrue(os.path.isfile(os.path.join(tmpdirname, "pytorch_lora_weights.bin")))
                pipe.unload_lora_weights()

                pipe.load_lora_weights(os.path.join(tmpdirname, "pytorch_lora_weights.bin"))

            images_lora_from_pretrained = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            self.assertTrue(
                np.allclose(images_lora, images_lora_from_pretrained, atol=1e-3, rtol=1e-3),
                "Loading from saved checkpoints should give same results.",
            )

    def test_simple_inference_with_text_unet_lora_and_scale(self):
        """
        Tests a simple inference with lora attached on the text encoder + Unet + scale argument
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            pipe.unet.add_adapter(unet_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            output_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                not np.allclose(output_lora, output_no_lora, atol=1e-3, rtol=1e-3), "Lora should change the output"
            )

            output_lora_scale = pipe(
                **inputs, generator=torch.manual_seed(0), cross_attention_kwargs={"scale": 0.5}
            ).images
            self.assertTrue(
                not np.allclose(output_lora, output_lora_scale, atol=1e-3, rtol=1e-3),
                "Lora + scale should change the output",
            )

            output_lora_0_scale = pipe(
                **inputs, generator=torch.manual_seed(0), cross_attention_kwargs={"scale": 0.0}
            ).images
            self.assertTrue(
                np.allclose(output_no_lora, output_lora_0_scale, atol=1e-3, rtol=1e-3),
                "Lora + 0 scale should lead to same result as no LoRA",
            )

            self.assertTrue(
                pipe.text_encoder.text_model.encoder.layers[0].self_attn.q_proj.scaling["default"] == 1.0,
                "The scaling parameter has not been correctly restored!",
            )

    def test_simple_inference_with_text_lora_unet_fused(self):
        """
        Tests a simple inference with lora attached into text encoder + fuses the lora weights into base model
        and makes sure it works as expected - with unet
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            pipe.unet.add_adapter(unet_lora_config)

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.fuse_lora()
            # Fusing should still keep the LoRA layers
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in unet")

            if self.has_two_text_encoders:
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            ouput_fused = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertFalse(
                np.allclose(ouput_fused, output_no_lora, atol=1e-3, rtol=1e-3), "Fused lora should change the output"
            )

    def test_simple_inference_with_text_unet_lora_unloaded(self):
        """
        Tests a simple inference with lora attached to text encoder and unet, then unloads the lora weights
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config)
            pipe.unet.add_adapter(unet_lora_config)
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.unload_lora_weights()
            # unloading should remove the LoRA layers
            self.assertFalse(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly unloaded in text encoder"
            )
            self.assertFalse(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly unloaded in Unet")

            if self.has_two_text_encoders:
                self.assertFalse(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2),
                    "Lora not correctly unloaded in text encoder 2",
                )

            ouput_unloaded = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                np.allclose(ouput_unloaded, output_no_lora, atol=1e-3, rtol=1e-3),
                "Fused lora should change the output",
            )

    def test_simple_inference_with_text_unet_lora_unfused(self):
        """
        Tests a simple inference with lora attached to text encoder and unet, then unloads the lora weights
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            pipe.text_encoder.add_adapter(text_lora_config)
            pipe.unet.add_adapter(unet_lora_config)

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.fuse_lora()

            output_fused_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.unfuse_lora()

            output_unfused_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            # unloading should remove the LoRA layers
            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Unfuse should still keep LoRA layers"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Unfuse should still keep LoRA layers")

            if self.has_two_text_encoders:
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Unfuse should still keep LoRA layers"
                )

            # Fuse and unfuse should lead to the same results
            self.assertTrue(
                np.allclose(output_fused_lora, output_unfused_lora, atol=1e-3, rtol=1e-3),
                "Fused lora should change the output",
            )

    def test_simple_inference_with_text_unet_multi_adapter(self):
        """
        Tests a simple inference with lora attached to text encoder and unet, attaches
        multiple adapters and set them
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")
            pipe.text_encoder.add_adapter(text_lora_config, "adapter-2")

            pipe.unet.add_adapter(unet_lora_config, "adapter-1")
            pipe.unet.add_adapter(unet_lora_config, "adapter-2")

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-1")
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-2")
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.set_adapters("adapter-1")

            output_adapter_1 = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.set_adapters("adapter-2")
            output_adapter_2 = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.set_adapters(["adapter-1", "adapter-2"])

            output_adapter_mixed = pipe(**inputs, generator=torch.manual_seed(0)).images

            # Fuse and unfuse should lead to the same results
            self.assertFalse(
                np.allclose(output_adapter_1, output_adapter_2, atol=1e-3, rtol=1e-3),
                "Adapter 1 and 2 should give different results",
            )

            self.assertFalse(
                np.allclose(output_adapter_1, output_adapter_mixed, atol=1e-3, rtol=1e-3),
                "Adapter 1 and mixed adapters should give different results",
            )

            self.assertFalse(
                np.allclose(output_adapter_2, output_adapter_mixed, atol=1e-3, rtol=1e-3),
                "Adapter 2 and mixed adapters should give different results",
            )

            pipe.disable_lora()

            output_disabled = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertTrue(
                np.allclose(output_no_lora, output_disabled, atol=1e-3, rtol=1e-3),
                "output with no lora and output with lora disabled should give same results",
            )

    def test_simple_inference_with_text_unet_multi_adapter_delete_adapter(self):
        """
        Tests a simple inference with lora attached to text encoder and unet, attaches
        multiple adapters and set/delete them
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")
            pipe.text_encoder.add_adapter(text_lora_config, "adapter-2")

            pipe.unet.add_adapter(unet_lora_config, "adapter-1")
            pipe.unet.add_adapter(unet_lora_config, "adapter-2")

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-1")
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-2")
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.set_adapters("adapter-1")

            output_adapter_1 = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.set_adapters("adapter-2")
            output_adapter_2 = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.set_adapters(["adapter-1", "adapter-2"])

            output_adapter_mixed = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertFalse(
                np.allclose(output_adapter_1, output_adapter_2, atol=1e-3, rtol=1e-3),
                "Adapter 1 and 2 should give different results",
            )

            self.assertFalse(
                np.allclose(output_adapter_1, output_adapter_mixed, atol=1e-3, rtol=1e-3),
                "Adapter 1 and mixed adapters should give different results",
            )

            self.assertFalse(
                np.allclose(output_adapter_2, output_adapter_mixed, atol=1e-3, rtol=1e-3),
                "Adapter 2 and mixed adapters should give different results",
            )

            pipe.delete_adapters("adapter-1")
            output_deleted_adapter_1 = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertTrue(
                np.allclose(output_deleted_adapter_1, output_adapter_2, atol=1e-3, rtol=1e-3),
                "Adapter 1 and 2 should give different results",
            )

            pipe.delete_adapters("adapter-2")
            output_deleted_adapters = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertTrue(
                np.allclose(output_no_lora, output_deleted_adapters, atol=1e-3, rtol=1e-3),
                "output with no lora and output with lora disabled should give same results",
            )

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")
            pipe.text_encoder.add_adapter(text_lora_config, "adapter-2")

            pipe.unet.add_adapter(unet_lora_config, "adapter-1")
            pipe.unet.add_adapter(unet_lora_config, "adapter-2")

            pipe.set_adapters(["adapter-1", "adapter-2"])
            pipe.delete_adapters(["adapter-1", "adapter-2"])

            output_deleted_adapters = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertTrue(
                np.allclose(output_no_lora, output_deleted_adapters, atol=1e-3, rtol=1e-3),
                "output with no lora and output with lora disabled should give same results",
            )

    def test_simple_inference_with_text_unet_multi_adapter_weighted(self):
        """
        Tests a simple inference with lora attached to text encoder and unet, attaches
        multiple adapters and set them
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")
            pipe.text_encoder.add_adapter(text_lora_config, "adapter-2")

            pipe.unet.add_adapter(unet_lora_config, "adapter-1")
            pipe.unet.add_adapter(unet_lora_config, "adapter-2")

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-1")
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-2")
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.set_adapters("adapter-1")

            output_adapter_1 = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.set_adapters("adapter-2")
            output_adapter_2 = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.set_adapters(["adapter-1", "adapter-2"])

            output_adapter_mixed = pipe(**inputs, generator=torch.manual_seed(0)).images

            # Fuse and unfuse should lead to the same results
            self.assertFalse(
                np.allclose(output_adapter_1, output_adapter_2, atol=1e-3, rtol=1e-3),
                "Adapter 1 and 2 should give different results",
            )

            self.assertFalse(
                np.allclose(output_adapter_1, output_adapter_mixed, atol=1e-3, rtol=1e-3),
                "Adapter 1 and mixed adapters should give different results",
            )

            self.assertFalse(
                np.allclose(output_adapter_2, output_adapter_mixed, atol=1e-3, rtol=1e-3),
                "Adapter 2 and mixed adapters should give different results",
            )

            pipe.set_adapters(["adapter-1", "adapter-2"], [0.5, 0.6])
            output_adapter_mixed_weighted = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertFalse(
                np.allclose(output_adapter_mixed_weighted, output_adapter_mixed, atol=1e-3, rtol=1e-3),
                "Weighted adapter and mixed adapter should give different results",
            )

            pipe.disable_lora()

            output_disabled = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertTrue(
                np.allclose(output_no_lora, output_disabled, atol=1e-3, rtol=1e-3),
                "output with no lora and output with lora disabled should give same results",
            )

    def test_lora_fuse_nan(self):
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")

            pipe.unet.add_adapter(unet_lora_config, "adapter-1")

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            # corrupt one LoRA weight with `inf` values
            with torch.no_grad():
                pipe.unet.mid_block.attentions[0].transformer_blocks[0].attn1.to_q.lora_A["adapter-1"].weight += float(
                    "inf"
                )

            # with `safe_fusing=True` we should see an Error
            with self.assertRaises(ValueError):
                pipe.fuse_lora(safe_fusing=True)

            # without we should not see an error, but every image will be black
            pipe.fuse_lora(safe_fusing=False)

            out = pipe("test", num_inference_steps=2, output_type="np").images

            self.assertTrue(np.isnan(out).all())

    def test_get_adapters(self):
        """
        Tests a simple usecase where we attach multiple adapters and check if the results
        are the expected results
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")
            pipe.unet.add_adapter(unet_lora_config, "adapter-1")

            adapter_names = pipe.get_active_adapters()
            self.assertListEqual(adapter_names, ["adapter-1"])

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-2")
            pipe.unet.add_adapter(unet_lora_config, "adapter-2")

            adapter_names = pipe.get_active_adapters()
            self.assertListEqual(adapter_names, ["adapter-2"])

            pipe.set_adapters(["adapter-1", "adapter-2"])
            self.assertListEqual(pipe.get_active_adapters(), ["adapter-1", "adapter-2"])

    def test_get_list_adapters(self):
        """
        Tests a simple usecase where we attach multiple adapters and check if the results
        are the expected results
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")
            pipe.unet.add_adapter(unet_lora_config, "adapter-1")

            adapter_names = pipe.get_list_adapters()
            self.assertDictEqual(adapter_names, {"text_encoder": ["adapter-1"], "unet": ["adapter-1"]})

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-2")
            pipe.unet.add_adapter(unet_lora_config, "adapter-2")

            adapter_names = pipe.get_list_adapters()
            self.assertDictEqual(
                adapter_names, {"text_encoder": ["adapter-1", "adapter-2"], "unet": ["adapter-1", "adapter-2"]}
            )

            pipe.set_adapters(["adapter-1", "adapter-2"])
            self.assertDictEqual(
                pipe.get_list_adapters(),
                {"unet": ["adapter-1", "adapter-2"], "text_encoder": ["adapter-1", "adapter-2"]},
            )

            pipe.unet.add_adapter(unet_lora_config, "adapter-3")
            self.assertDictEqual(
                pipe.get_list_adapters(),
                {"unet": ["adapter-1", "adapter-2", "adapter-3"], "text_encoder": ["adapter-1", "adapter-2"]},
            )

    @require_peft_version_greater(peft_version="0.6.2")
    def test_simple_inference_with_text_lora_unet_fused_multi(self):
        """
        Tests a simple inference with lora attached into text encoder + fuses the lora weights into base model
        and makes sure it works as expected - with unet and multi-adapter case
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            output_no_lora = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(output_no_lora.shape == (1, 64, 64, 3))

            pipe.text_encoder.add_adapter(text_lora_config, "adapter-1")
            pipe.unet.add_adapter(unet_lora_config, "adapter-1")

            # Attach a second adapter
            pipe.text_encoder.add_adapter(text_lora_config, "adapter-2")
            pipe.unet.add_adapter(unet_lora_config, "adapter-2")

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-1")
                pipe.text_encoder_2.add_adapter(text_lora_config, "adapter-2")
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            # set them to multi-adapter inference mode
            pipe.set_adapters(["adapter-1", "adapter-2"])
            ouputs_all_lora = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.set_adapters(["adapter-1"])
            ouputs_lora_1 = pipe(**inputs, generator=torch.manual_seed(0)).images

            pipe.fuse_lora(adapter_names=["adapter-1"])

            # Fusing should still keep the LoRA layers so outpout should remain the same
            outputs_lora_1_fused = pipe(**inputs, generator=torch.manual_seed(0)).images

            self.assertTrue(
                np.allclose(ouputs_lora_1, outputs_lora_1_fused, atol=1e-3, rtol=1e-3),
                "Fused lora should not change the output",
            )

            pipe.unfuse_lora()
            pipe.fuse_lora(adapter_names=["adapter-2", "adapter-1"])

            # Fusing should still keep the LoRA layers
            output_all_lora_fused = pipe(**inputs, generator=torch.manual_seed(0)).images
            self.assertTrue(
                np.allclose(output_all_lora_fused, ouputs_all_lora, atol=1e-3, rtol=1e-3),
                "Fused lora should not change the output",
            )

    @unittest.skip("This is failing for now - need to investigate")
    def test_simple_inference_with_text_unet_lora_unfused_torch_compile(self):
        """
        Tests a simple inference with lora attached to text encoder and unet, then unloads the lora weights
        and makes sure it works as expected
        """
        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, text_lora_config, unet_lora_config = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _, _, inputs = self.get_dummy_inputs(with_generator=False)

            pipe.text_encoder.add_adapter(text_lora_config)
            pipe.unet.add_adapter(unet_lora_config)

            self.assertTrue(
                self.check_if_lora_correctly_set(pipe.text_encoder), "Lora not correctly set in text encoder"
            )
            self.assertTrue(self.check_if_lora_correctly_set(pipe.unet), "Lora not correctly set in Unet")

            if self.has_two_text_encoders:
                pipe.text_encoder_2.add_adapter(text_lora_config)
                self.assertTrue(
                    self.check_if_lora_correctly_set(pipe.text_encoder_2), "Lora not correctly set in text encoder 2"
                )

            pipe.unet = torch.compile(pipe.unet, mode="reduce-overhead", fullgraph=True)
            pipe.text_encoder = torch.compile(pipe.text_encoder, mode="reduce-overhead", fullgraph=True)

            if self.has_two_text_encoders:
                pipe.text_encoder_2 = torch.compile(pipe.text_encoder_2, mode="reduce-overhead", fullgraph=True)

            # Just makes sure it works..
            _ = pipe(**inputs, generator=torch.manual_seed(0)).images

    def test_modify_padding_mode(self):
        def set_pad_mode(network, mode="circular"):
            for _, module in network.named_modules():
                if isinstance(module, torch.nn.Conv2d):
                    module.padding_mode = mode

        for scheduler_cls in [DDIMScheduler, LCMScheduler]:
            components, _, _ = self.get_dummy_components(scheduler_cls)
            pipe = self.pipeline_class(**components)
            pipe = pipe.to(self.torch_device)
            pipe.set_progress_bar_config(disable=None)
            _pad_mode = "circular"
            set_pad_mode(pipe.vae, _pad_mode)
            set_pad_mode(pipe.unet, _pad_mode)

            _, _, inputs = self.get_dummy_inputs()
            _ = pipe(**inputs).images


class StableDiffusionLoRATests(PeftLoraLoaderMixinTests, unittest.TestCase):
    pipeline_class = StableDiffusionPipeline
    scheduler_cls = DDIMScheduler
    scheduler_kwargs = {
        "beta_start": 0.00085,
        "beta_end": 0.012,
        "beta_schedule": "scaled_linear",
        "clip_sample": False,
        "set_alpha_to_one": False,
        "steps_offset": 1,
    }
    unet_kwargs = {
        "block_out_channels": (32, 64),
        "layers_per_block": 2,
        "sample_size": 32,
        "in_channels": 4,
        "out_channels": 4,
        "down_block_types": ("DownBlock2D", "CrossAttnDownBlock2D"),
        "up_block_types": ("CrossAttnUpBlock2D", "UpBlock2D"),
        "cross_attention_dim": 32,
    }
    vae_kwargs = {
        "block_out_channels": [32, 64],
        "in_channels": 3,
        "out_channels": 3,
        "down_block_types": ["DownEncoderBlock2D", "DownEncoderBlock2D"],
        "up_block_types": ["UpDecoderBlock2D", "UpDecoderBlock2D"],
        "latent_channels": 4,
    }

    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    @slow
    @require_torch_gpu
    def test_integration_move_lora_cpu(self):
        path = "runwayml/stable-diffusion-v1-5"
        lora_id = "takuma104/lora-test-text-encoder-lora-target"

        pipe = StableDiffusionPipeline.from_pretrained(path, torch_dtype=torch.float16)
        pipe.load_lora_weights(lora_id, adapter_name="adapter-1")
        pipe.load_lora_weights(lora_id, adapter_name="adapter-2")
        pipe = pipe.to("cuda")

        self.assertTrue(
            self.check_if_lora_correctly_set(pipe.text_encoder),
            "Lora not correctly set in text encoder",
        )

        self.assertTrue(
            self.check_if_lora_correctly_set(pipe.unet),
            "Lora not correctly set in text encoder",
        )

        # We will offload the first adapter in CPU and check if the offloading
        # has been performed correctly
        pipe.set_lora_device(["adapter-1"], "cpu")

        for name, module in pipe.unet.named_modules():
            if "adapter-1" in name and not isinstance(module, (nn.Dropout, nn.Identity)):
                self.assertTrue(module.weight.device == torch.device("cpu"))
            elif "adapter-2" in name and not isinstance(module, (nn.Dropout, nn.Identity)):
                self.assertTrue(module.weight.device != torch.device("cpu"))

        for name, module in pipe.text_encoder.named_modules():
            if "adapter-1" in name and not isinstance(module, (nn.Dropout, nn.Identity)):
                self.assertTrue(module.weight.device == torch.device("cpu"))
            elif "adapter-2" in name and not isinstance(module, (nn.Dropout, nn.Identity)):
                self.assertTrue(module.weight.device != torch.device("cpu"))

        pipe.set_lora_device(["adapter-1"], 0)

        for n, m in pipe.unet.named_modules():
            if "adapter-1" in n and not isinstance(m, (nn.Dropout, nn.Identity)):
                self.assertTrue(m.weight.device != torch.device("cpu"))

        for n, m in pipe.text_encoder.named_modules():
            if "adapter-1" in n and not isinstance(m, (nn.Dropout, nn.Identity)):
                self.assertTrue(m.weight.device != torch.device("cpu"))

        pipe.set_lora_device(["adapter-1", "adapter-2"], "cuda")

        for n, m in pipe.unet.named_modules():
            if ("adapter-1" in n or "adapter-2" in n) and not isinstance(m, (nn.Dropout, nn.Identity)):
                self.assertTrue(m.weight.device != torch.device("cpu"))

        for n, m in pipe.text_encoder.named_modules():
            if ("adapter-1" in n or "adapter-2" in n) and not isinstance(m, (nn.Dropout, nn.Identity)):
                self.assertTrue(m.weight.device != torch.device("cpu"))

    @slow
    @require_torch_gpu
    def test_integration_logits_with_scale(self):
        path = "runwayml/stable-diffusion-v1-5"
        lora_id = "takuma104/lora-test-text-encoder-lora-target"

        pipe = StableDiffusionPipeline.from_pretrained(path, torch_dtype=torch.float32)
        pipe.load_lora_weights(lora_id)
        pipe = pipe.to("cuda")

        self.assertTrue(
            self.check_if_lora_correctly_set(pipe.text_encoder),
            "Lora not correctly set in text encoder 2",
        )

        prompt = "a red sks dog"

        images = pipe(
            prompt=prompt,
            num_inference_steps=15,
            cross_attention_kwargs={"scale": 0.5},
            generator=torch.manual_seed(0),
            output_type="np",
        ).images

        expected_slice_scale = np.array([0.307, 0.283, 0.310, 0.310, 0.300, 0.314, 0.336, 0.314, 0.321])

        predicted_slice = images[0, -3:, -3:, -1].flatten()

        self.assertTrue(np.allclose(expected_slice_scale, predicted_slice, atol=1e-3, rtol=1e-3))

    @slow
    @require_torch_gpu
    def test_integration_logits_no_scale(self):
        path = "runwayml/stable-diffusion-v1-5"
        lora_id = "takuma104/lora-test-text-encoder-lora-target"

        pipe = StableDiffusionPipeline.from_pretrained(path, torch_dtype=torch.float32)
        pipe.load_lora_weights(lora_id)
        pipe = pipe.to("cuda")

        self.assertTrue(
            self.check_if_lora_correctly_set(pipe.text_encoder),
            "Lora not correctly set in text encoder",
        )

        prompt = "a red sks dog"

        images = pipe(prompt=prompt, num_inference_steps=30, generator=torch.manual_seed(0), output_type="np").images

        expected_slice_scale = np.array([0.074, 0.064, 0.073, 0.0842, 0.069, 0.0641, 0.0794, 0.076, 0.084])

        predicted_slice = images[0, -3:, -3:, -1].flatten()

        self.assertTrue(np.allclose(expected_slice_scale, predicted_slice, atol=1e-3, rtol=1e-3))

    @nightly
    @require_torch_gpu
    def test_integration_logits_multi_adapter(self):
        path = "stabilityai/stable-diffusion-xl-base-1.0"
        lora_id = "CiroN2022/toy-face"

        pipe = StableDiffusionXLPipeline.from_pretrained(path, torch_dtype=torch.float16)
        pipe.load_lora_weights(lora_id, weight_name="toy_face_sdxl.safetensors", adapter_name="toy")
        pipe = pipe.to("cuda")

        self.assertTrue(
            self.check_if_lora_correctly_set(pipe.unet),
            "Lora not correctly set in Unet",
        )

        prompt = "toy_face of a hacker with a hoodie"

        lora_scale = 0.9

        images = pipe(
            prompt=prompt,
            num_inference_steps=30,
            generator=torch.manual_seed(0),
            cross_attention_kwargs={"scale": lora_scale},
            output_type="np",
        ).images
        expected_slice_scale = np.array([0.538, 0.539, 0.540, 0.540, 0.542, 0.539, 0.538, 0.541, 0.539])

        predicted_slice = images[0, -3:, -3:, -1].flatten()
        self.assertTrue(np.allclose(expected_slice_scale, predicted_slice, atol=1e-3, rtol=1e-3))

        pipe.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors", adapter_name="pixel")
        pipe.set_adapters("pixel")

        prompt = "pixel art, a hacker with a hoodie, simple, flat colors"
        images = pipe(
            prompt,
            num_inference_steps=30,
            guidance_scale=7.5,
            cross_attention_kwargs={"scale": lora_scale},
            generator=torch.manual_seed(0),
            output_type="np",
        ).images

        predicted_slice = images[0, -3:, -3:, -1].flatten()
        expected_slice_scale = np.array(
            [0.61973065, 0.62018543, 0.62181497, 0.61933696, 0.6208608, 0.620576, 0.6200281, 0.62258327, 0.6259889]
        )
        self.assertTrue(np.allclose(expected_slice_scale, predicted_slice, atol=1e-3, rtol=1e-3))

        # multi-adapter inference
        pipe.set_adapters(["pixel", "toy"], adapter_weights=[0.5, 1.0])
        images = pipe(
            prompt,
            num_inference_steps=30,
            guidance_scale=7.5,
            cross_attention_kwargs={"scale": 1.0},
            generator=torch.manual_seed(0),
            output_type="np",
        ).images
        predicted_slice = images[0, -3:, -3:, -1].flatten()
        expected_slice_scale = np.array([0.5888, 0.5897, 0.5946, 0.5888, 0.5935, 0.5946, 0.5857, 0.5891, 0.5909])
        self.assertTrue(np.allclose(expected_slice_scale, predicted_slice, atol=1e-3, rtol=1e-3))

        # Lora disabled
        pipe.disable_lora()
        images = pipe(
            prompt,
            num_inference_steps=30,
            guidance_scale=7.5,
            cross_attention_kwargs={"scale": lora_scale},
            generator=torch.manual_seed(0),
            output_type="np",
        ).images
        predicted_slice = images[0, -3:, -3:, -1].flatten()
        expected_slice_scale = np.array([0.5456, 0.5466, 0.5487, 0.5458, 0.5469, 0.5454, 0.5446, 0.5479, 0.5487])
        self.assertTrue(np.allclose(expected_slice_scale, predicted_slice, atol=1e-3, rtol=1e-3))


class StableDiffusionXLLoRATests(PeftLoraLoaderMixinTests, unittest.TestCase):
    has_two_text_encoders = True
    pipeline_class = StableDiffusionXLPipeline
    scheduler_cls = EulerDiscreteScheduler
    scheduler_kwargs = {
        "beta_start": 0.00085,
        "beta_end": 0.012,
        "beta_schedule": "scaled_linear",
        "timestep_spacing": "leading",
        "steps_offset": 1,
    }
    unet_kwargs = {
        "block_out_channels": (32, 64),
        "layers_per_block": 2,
        "sample_size": 32,
        "in_channels": 4,
        "out_channels": 4,
        "down_block_types": ("DownBlock2D", "CrossAttnDownBlock2D"),
        "up_block_types": ("CrossAttnUpBlock2D", "UpBlock2D"),
        "attention_head_dim": (2, 4),
        "use_linear_projection": True,
        "addition_embed_type": "text_time",
        "addition_time_embed_dim": 8,
        "transformer_layers_per_block": (1, 2),
        "projection_class_embeddings_input_dim": 80,  # 6 * 8 + 32
        "cross_attention_dim": 64,
    }
    vae_kwargs = {
        "block_out_channels": [32, 64],
        "in_channels": 3,
        "out_channels": 3,
        "down_block_types": ["DownEncoderBlock2D", "DownEncoderBlock2D"],
        "up_block_types": ["UpDecoderBlock2D", "UpDecoderBlock2D"],
        "latent_channels": 4,
        "sample_size": 128,
    }

    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()


@slow
@require_torch_gpu
class LoraIntegrationTests(PeftLoraLoaderMixinTests, unittest.TestCase):
    pipeline_class = StableDiffusionPipeline
    scheduler_cls = DDIMScheduler
    scheduler_kwargs = {
        "beta_start": 0.00085,
        "beta_end": 0.012,
        "beta_schedule": "scaled_linear",
        "clip_sample": False,
        "set_alpha_to_one": False,
        "steps_offset": 1,
    }
    unet_kwargs = {
        "block_out_channels": (32, 64),
        "layers_per_block": 2,
        "sample_size": 32,
        "in_channels": 4,
        "out_channels": 4,
        "down_block_types": ("DownBlock2D", "CrossAttnDownBlock2D"),
        "up_block_types": ("CrossAttnUpBlock2D", "UpBlock2D"),
        "cross_attention_dim": 32,
    }
    vae_kwargs = {
        "block_out_channels": [32, 64],
        "in_channels": 3,
        "out_channels": 3,
        "down_block_types": ["DownEncoderBlock2D", "DownEncoderBlock2D"],
        "up_block_types": ["UpDecoderBlock2D", "UpDecoderBlock2D"],
        "latent_channels": 4,
    }

    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def test_dreambooth_old_format(self):
        generator = torch.Generator("cpu").manual_seed(0)

        lora_model_id = "hf-internal-testing/lora_dreambooth_dog_example"
        card = RepoCard.load(lora_model_id)
        base_model_id = card.data.to_dict()["base_model"]

        pipe = StableDiffusionPipeline.from_pretrained(base_model_id, safety_checker=None)
        pipe = pipe.to(torch_device)
        pipe.load_lora_weights(lora_model_id)

        images = pipe(
            "A photo of a sks dog floating in the river", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()

        expected = np.array([0.7207, 0.6787, 0.6010, 0.7478, 0.6838, 0.6064, 0.6984, 0.6443, 0.5785])

        self.assertTrue(np.allclose(images, expected, atol=1e-4))
        release_memory(pipe)

    def test_dreambooth_text_encoder_new_format(self):
        generator = torch.Generator().manual_seed(0)

        lora_model_id = "hf-internal-testing/lora-trained"
        card = RepoCard.load(lora_model_id)
        base_model_id = card.data.to_dict()["base_model"]

        pipe = StableDiffusionPipeline.from_pretrained(base_model_id, safety_checker=None)
        pipe = pipe.to(torch_device)
        pipe.load_lora_weights(lora_model_id)

        images = pipe("A photo of a sks dog", output_type="np", generator=generator, num_inference_steps=2).images

        images = images[0, -3:, -3:, -1].flatten()

        expected = np.array([0.6628, 0.6138, 0.5390, 0.6625, 0.6130, 0.5463, 0.6166, 0.5788, 0.5359])

        self.assertTrue(np.allclose(images, expected, atol=1e-4))
        release_memory(pipe)

    def test_a1111(self):
        generator = torch.Generator().manual_seed(0)

        pipe = StableDiffusionPipeline.from_pretrained("hf-internal-testing/Counterfeit-V2.5", safety_checker=None).to(
            torch_device
        )
        lora_model_id = "hf-internal-testing/civitai-light-shadow-lora"
        lora_filename = "light_and_shadow.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.3636, 0.3708, 0.3694, 0.3679, 0.3829, 0.3677, 0.3692, 0.3688, 0.3292])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_lycoris(self):
        generator = torch.Generator().manual_seed(0)

        pipe = StableDiffusionPipeline.from_pretrained(
            "hf-internal-testing/Amixx", safety_checker=None, use_safetensors=True, variant="fp16"
        ).to(torch_device)
        lora_model_id = "hf-internal-testing/edgLycorisMugler-light"
        lora_filename = "edgLycorisMugler-light.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.6463, 0.658, 0.599, 0.6542, 0.6512, 0.6213, 0.658, 0.6485, 0.6017])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_a1111_with_model_cpu_offload(self):
        generator = torch.Generator().manual_seed(0)

        pipe = StableDiffusionPipeline.from_pretrained("hf-internal-testing/Counterfeit-V2.5", safety_checker=None)
        pipe.enable_model_cpu_offload()
        lora_model_id = "hf-internal-testing/civitai-light-shadow-lora"
        lora_filename = "light_and_shadow.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.3636, 0.3708, 0.3694, 0.3679, 0.3829, 0.3677, 0.3692, 0.3688, 0.3292])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_a1111_with_sequential_cpu_offload(self):
        generator = torch.Generator().manual_seed(0)

        pipe = StableDiffusionPipeline.from_pretrained("hf-internal-testing/Counterfeit-V2.5", safety_checker=None)
        pipe.enable_sequential_cpu_offload()
        lora_model_id = "hf-internal-testing/civitai-light-shadow-lora"
        lora_filename = "light_and_shadow.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.3636, 0.3708, 0.3694, 0.3679, 0.3829, 0.3677, 0.3692, 0.3688, 0.3292])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_kohya_sd_v15_with_higher_dimensions(self):
        generator = torch.Generator().manual_seed(0)

        pipe = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", safety_checker=None).to(
            torch_device
        )
        lora_model_id = "hf-internal-testing/urushisato-lora"
        lora_filename = "urushisato_v15.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.7165, 0.6616, 0.5833, 0.7504, 0.6718, 0.587, 0.6871, 0.6361, 0.5694])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_vanilla_funetuning(self):
        generator = torch.Generator().manual_seed(0)

        lora_model_id = "hf-internal-testing/sd-model-finetuned-lora-t4"
        card = RepoCard.load(lora_model_id)
        base_model_id = card.data.to_dict()["base_model"]

        pipe = StableDiffusionPipeline.from_pretrained(base_model_id, safety_checker=None)
        pipe = pipe.to(torch_device)
        pipe.load_lora_weights(lora_model_id)

        images = pipe("A pokemon with blue eyes.", output_type="np", generator=generator, num_inference_steps=2).images

        images = images[0, -3:, -3:, -1].flatten()

        expected = np.array([0.7406, 0.699, 0.5963, 0.7493, 0.7045, 0.6096, 0.6886, 0.6388, 0.583])

        self.assertTrue(np.allclose(images, expected, atol=1e-4))
        release_memory(pipe)

    def test_unload_kohya_lora(self):
        generator = torch.manual_seed(0)
        prompt = "masterpiece, best quality, mountain"
        num_inference_steps = 2

        pipe = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", safety_checker=None).to(
            torch_device
        )
        initial_images = pipe(
            prompt, output_type="np", generator=generator, num_inference_steps=num_inference_steps
        ).images
        initial_images = initial_images[0, -3:, -3:, -1].flatten()

        lora_model_id = "hf-internal-testing/civitai-colored-icons-lora"
        lora_filename = "Colored_Icons_by_vizsumit.safetensors"

        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        generator = torch.manual_seed(0)
        lora_images = pipe(
            prompt, output_type="np", generator=generator, num_inference_steps=num_inference_steps
        ).images
        lora_images = lora_images[0, -3:, -3:, -1].flatten()

        pipe.unload_lora_weights()
        generator = torch.manual_seed(0)
        unloaded_lora_images = pipe(
            prompt, output_type="np", generator=generator, num_inference_steps=num_inference_steps
        ).images
        unloaded_lora_images = unloaded_lora_images[0, -3:, -3:, -1].flatten()

        self.assertFalse(np.allclose(initial_images, lora_images))
        self.assertTrue(np.allclose(initial_images, unloaded_lora_images, atol=1e-3))
        release_memory(pipe)

    def test_load_unload_load_kohya_lora(self):
        # This test ensures that a Kohya-style LoRA can be safely unloaded and then loaded
        # without introducing any side-effects. Even though the test uses a Kohya-style
        # LoRA, the underlying adapter handling mechanism is format-agnostic.
        generator = torch.manual_seed(0)
        prompt = "masterpiece, best quality, mountain"
        num_inference_steps = 2

        pipe = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", safety_checker=None).to(
            torch_device
        )
        initial_images = pipe(
            prompt, output_type="np", generator=generator, num_inference_steps=num_inference_steps
        ).images
        initial_images = initial_images[0, -3:, -3:, -1].flatten()

        lora_model_id = "hf-internal-testing/civitai-colored-icons-lora"
        lora_filename = "Colored_Icons_by_vizsumit.safetensors"

        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        generator = torch.manual_seed(0)
        lora_images = pipe(
            prompt, output_type="np", generator=generator, num_inference_steps=num_inference_steps
        ).images
        lora_images = lora_images[0, -3:, -3:, -1].flatten()

        pipe.unload_lora_weights()
        generator = torch.manual_seed(0)
        unloaded_lora_images = pipe(
            prompt, output_type="np", generator=generator, num_inference_steps=num_inference_steps
        ).images
        unloaded_lora_images = unloaded_lora_images[0, -3:, -3:, -1].flatten()

        self.assertFalse(np.allclose(initial_images, lora_images))
        self.assertTrue(np.allclose(initial_images, unloaded_lora_images, atol=1e-3))

        # make sure we can load a LoRA again after unloading and they don't have
        # any undesired effects.
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        generator = torch.manual_seed(0)
        lora_images_again = pipe(
            prompt, output_type="np", generator=generator, num_inference_steps=num_inference_steps
        ).images
        lora_images_again = lora_images_again[0, -3:, -3:, -1].flatten()

        self.assertTrue(np.allclose(lora_images, lora_images_again, atol=1e-3))
        release_memory(pipe)

    def test_not_empty_state_dict(self):
        # Makes sure https://github.com/huggingface/diffusers/issues/7054 does not happen again
        pipe = AutoPipelineForText2Image.from_pretrained(
            "runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16
        ).to("cuda")
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)

        cached_file = hf_hub_download("hf-internal-testing/lcm-lora-test-sd-v1-5", "test_lora.safetensors")
        lcm_lora = load_file(cached_file)

        pipe.load_lora_weights(lcm_lora, adapter_name="lcm")
        self.assertTrue(lcm_lora != {})
        release_memory(pipe)

    def test_load_unload_load_state_dict(self):
        # Makes sure https://github.com/huggingface/diffusers/issues/7054 does not happen again
        pipe = AutoPipelineForText2Image.from_pretrained(
            "runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16
        ).to("cuda")
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)

        cached_file = hf_hub_download("hf-internal-testing/lcm-lora-test-sd-v1-5", "test_lora.safetensors")
        lcm_lora = load_file(cached_file)
        previous_state_dict = lcm_lora.copy()

        pipe.load_lora_weights(lcm_lora, adapter_name="lcm")
        self.assertDictEqual(lcm_lora, previous_state_dict)

        pipe.unload_lora_weights()
        pipe.load_lora_weights(lcm_lora, adapter_name="lcm")
        self.assertDictEqual(lcm_lora, previous_state_dict)

        release_memory(pipe)


@slow
@require_torch_gpu
class LoraSDXLIntegrationTests(PeftLoraLoaderMixinTests, unittest.TestCase):
    has_two_text_encoders = True
    pipeline_class = StableDiffusionXLPipeline
    scheduler_cls = EulerDiscreteScheduler
    scheduler_kwargs = {
        "beta_start": 0.00085,
        "beta_end": 0.012,
        "beta_schedule": "scaled_linear",
        "timestep_spacing": "leading",
        "steps_offset": 1,
    }
    unet_kwargs = {
        "block_out_channels": (32, 64),
        "layers_per_block": 2,
        "sample_size": 32,
        "in_channels": 4,
        "out_channels": 4,
        "down_block_types": ("DownBlock2D", "CrossAttnDownBlock2D"),
        "up_block_types": ("CrossAttnUpBlock2D", "UpBlock2D"),
        "attention_head_dim": (2, 4),
        "use_linear_projection": True,
        "addition_embed_type": "text_time",
        "addition_time_embed_dim": 8,
        "transformer_layers_per_block": (1, 2),
        "projection_class_embeddings_input_dim": 80,  # 6 * 8 + 32
        "cross_attention_dim": 64,
    }
    vae_kwargs = {
        "block_out_channels": [32, 64],
        "in_channels": 3,
        "out_channels": 3,
        "down_block_types": ["DownEncoderBlock2D", "DownEncoderBlock2D"],
        "up_block_types": ["UpDecoderBlock2D", "UpDecoderBlock2D"],
        "latent_channels": 4,
        "sample_size": 128,
    }

    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def test_sdxl_0_9_lora_one(self):
        generator = torch.Generator().manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-0.9")
        lora_model_id = "hf-internal-testing/sdxl-0.9-daiton-lora"
        lora_filename = "daiton-xl-lora-test.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        pipe.enable_model_cpu_offload()

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.3838, 0.3482, 0.3588, 0.3162, 0.319, 0.3369, 0.338, 0.3366, 0.3213])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_sdxl_0_9_lora_two(self):
        generator = torch.Generator().manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-0.9")
        lora_model_id = "hf-internal-testing/sdxl-0.9-costumes-lora"
        lora_filename = "saijo.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        pipe.enable_model_cpu_offload()

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.3137, 0.3269, 0.3355, 0.255, 0.2577, 0.2563, 0.2679, 0.2758, 0.2626])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_sdxl_0_9_lora_three(self):
        generator = torch.Generator().manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-0.9")
        lora_model_id = "hf-internal-testing/sdxl-0.9-kamepan-lora"
        lora_filename = "kame_sdxl_v2-000020-16rank.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        pipe.enable_model_cpu_offload()

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.4015, 0.3761, 0.3616, 0.3745, 0.3462, 0.3337, 0.3564, 0.3649, 0.3468])

        self.assertTrue(np.allclose(images, expected, atol=5e-3))
        release_memory(pipe)

    def test_sdxl_1_0_lora(self):
        generator = torch.Generator("cpu").manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0")
        pipe.enable_model_cpu_offload()
        lora_model_id = "hf-internal-testing/sdxl-1.0-lora"
        lora_filename = "sd_xl_offset_example-lora_1.0.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.4468, 0.4087, 0.4134, 0.366, 0.3202, 0.3505, 0.3786, 0.387, 0.3535])

        self.assertTrue(np.allclose(images, expected, atol=1e-4))
        release_memory(pipe)

    def test_sdxl_lcm_lora(self):
        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16)
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
        pipe.enable_model_cpu_offload()

        generator = torch.Generator("cpu").manual_seed(0)

        lora_model_id = "latent-consistency/lcm-lora-sdxl"

        pipe.load_lora_weights(lora_model_id)

        image = pipe(
            "masterpiece, best quality, mountain", generator=generator, num_inference_steps=4, guidance_scale=0.5
        ).images[0]

        expected_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/lcm_lora/sdxl_lcm_lora.png"
        )

        image_np = pipe.image_processor.pil_to_numpy(image)
        expected_image_np = pipe.image_processor.pil_to_numpy(expected_image)

        max_diff = numpy_cosine_similarity_distance(image_np.flatten(), expected_image_np.flatten())
        assert max_diff < 1e-4

        pipe.unload_lora_weights()

        release_memory(pipe)

    def test_sdv1_5_lcm_lora(self):
        pipe = DiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16)
        pipe.to("cuda")
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)

        generator = torch.Generator("cpu").manual_seed(0)

        lora_model_id = "latent-consistency/lcm-lora-sdv1-5"
        pipe.load_lora_weights(lora_model_id)

        image = pipe(
            "masterpiece, best quality, mountain", generator=generator, num_inference_steps=4, guidance_scale=0.5
        ).images[0]

        expected_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/lcm_lora/sdv15_lcm_lora.png"
        )

        image_np = pipe.image_processor.pil_to_numpy(image)
        expected_image_np = pipe.image_processor.pil_to_numpy(expected_image)

        max_diff = numpy_cosine_similarity_distance(image_np.flatten(), expected_image_np.flatten())
        assert max_diff < 1e-4

        pipe.unload_lora_weights()

        release_memory(pipe)

    def test_sdv1_5_lcm_lora_img2img(self):
        pipe = AutoPipelineForImage2Image.from_pretrained("runwayml/stable-diffusion-v1-5", torch_dtype=torch.float16)
        pipe.to("cuda")
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)

        init_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/img2img/fantasy_landscape.png"
        )

        generator = torch.Generator("cpu").manual_seed(0)

        lora_model_id = "latent-consistency/lcm-lora-sdv1-5"
        pipe.load_lora_weights(lora_model_id)

        image = pipe(
            "snowy mountain",
            generator=generator,
            image=init_image,
            strength=0.5,
            num_inference_steps=4,
            guidance_scale=0.5,
        ).images[0]

        expected_image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/lcm_lora/sdv15_lcm_lora_img2img.png"
        )

        image_np = pipe.image_processor.pil_to_numpy(image)
        expected_image_np = pipe.image_processor.pil_to_numpy(expected_image)

        max_diff = numpy_cosine_similarity_distance(image_np.flatten(), expected_image_np.flatten())
        assert max_diff < 1e-4

        pipe.unload_lora_weights()

        release_memory(pipe)

    def test_sdxl_1_0_lora_fusion(self):
        generator = torch.Generator().manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0")
        lora_model_id = "hf-internal-testing/sdxl-1.0-lora"
        lora_filename = "sd_xl_offset_example-lora_1.0.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        pipe.fuse_lora()
        # We need to unload the lora weights since in the previous API `fuse_lora` led to lora weights being
        # silently deleted - otherwise this will CPU OOM
        pipe.unload_lora_weights()

        pipe.enable_model_cpu_offload()

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        # This way we also test equivalence between LoRA fusion and the non-fusion behaviour.
        expected = np.array([0.4468, 0.4087, 0.4134, 0.366, 0.3202, 0.3505, 0.3786, 0.387, 0.3535])

        self.assertTrue(np.allclose(images, expected, atol=1e-4))
        release_memory(pipe)

    def test_sdxl_1_0_lora_unfusion(self):
        generator = torch.Generator("cpu").manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0")
        lora_model_id = "hf-internal-testing/sdxl-1.0-lora"
        lora_filename = "sd_xl_offset_example-lora_1.0.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        pipe.fuse_lora()

        pipe.enable_model_cpu_offload()

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=3
        ).images
        images_with_fusion = images.flatten()

        pipe.unfuse_lora()
        generator = torch.Generator("cpu").manual_seed(0)
        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=3
        ).images
        images_without_fusion = images.flatten()

        max_diff = numpy_cosine_similarity_distance(images_with_fusion, images_without_fusion)
        assert max_diff < 1e-4

        release_memory(pipe)

    def test_sdxl_1_0_lora_unfusion_effectivity(self):
        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0")
        pipe.enable_model_cpu_offload()

        generator = torch.Generator().manual_seed(0)
        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images
        original_image_slice = images[0, -3:, -3:, -1].flatten()

        lora_model_id = "hf-internal-testing/sdxl-1.0-lora"
        lora_filename = "sd_xl_offset_example-lora_1.0.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)
        pipe.fuse_lora()

        generator = torch.Generator().manual_seed(0)
        _ = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        pipe.unfuse_lora()

        # We need to unload the lora weights - in the old API unfuse led to unloading the adapter weights
        pipe.unload_lora_weights()

        generator = torch.Generator().manual_seed(0)
        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images
        images_without_fusion_slice = images[0, -3:, -3:, -1].flatten()

        self.assertTrue(np.allclose(original_image_slice, images_without_fusion_slice, atol=1e-3))
        release_memory(pipe)

    def test_sdxl_1_0_lora_fusion_efficiency(self):
        generator = torch.Generator().manual_seed(0)
        lora_model_id = "hf-internal-testing/sdxl-1.0-lora"
        lora_filename = "sd_xl_offset_example-lora_1.0.safetensors"

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16)
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename, torch_dtype=torch.float16)
        pipe.enable_model_cpu_offload()

        start_time = time.time()
        for _ in range(3):
            pipe(
                "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
            ).images
        end_time = time.time()
        elapsed_time_non_fusion = end_time - start_time

        del pipe

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16)
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename, torch_dtype=torch.float16)
        pipe.fuse_lora()

        # We need to unload the lora weights since in the previous API `fuse_lora` led to lora weights being
        # silently deleted - otherwise this will CPU OOM
        pipe.unload_lora_weights()
        pipe.enable_model_cpu_offload()

        generator = torch.Generator().manual_seed(0)
        start_time = time.time()
        for _ in range(3):
            pipe(
                "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
            ).images
        end_time = time.time()
        elapsed_time_fusion = end_time - start_time

        self.assertTrue(elapsed_time_fusion < elapsed_time_non_fusion)
        release_memory(pipe)

    def test_sdxl_1_0_last_ben(self):
        generator = torch.Generator().manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0")
        pipe.enable_model_cpu_offload()
        lora_model_id = "TheLastBen/Papercut_SDXL"
        lora_filename = "papercut.safetensors"
        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe("papercut.safetensors", output_type="np", generator=generator, num_inference_steps=2).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.5244, 0.4347, 0.4312, 0.4246, 0.4398, 0.4409, 0.4884, 0.4938, 0.4094])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_sdxl_1_0_fuse_unfuse_all(self):
        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16)
        text_encoder_1_sd = copy.deepcopy(pipe.text_encoder.state_dict())
        text_encoder_2_sd = copy.deepcopy(pipe.text_encoder_2.state_dict())
        unet_sd = copy.deepcopy(pipe.unet.state_dict())

        pipe.load_lora_weights(
            "davizca87/sun-flower", weight_name="snfw3rXL-000004.safetensors", torch_dtype=torch.float16
        )

        fused_te_state_dict = pipe.text_encoder.state_dict()
        fused_te_2_state_dict = pipe.text_encoder_2.state_dict()
        unet_state_dict = pipe.unet.state_dict()

        peft_ge_070 = version.parse(importlib.metadata.version("peft")) >= version.parse("0.7.0")

        def remap_key(key, sd):
            # some keys have moved around for PEFT >= 0.7.0, but they should still be loaded correctly
            if (key in sd) or (not peft_ge_070):
                return key

            # instead of linear.weight, we now have linear.base_layer.weight, etc.
            if key.endswith(".weight"):
                key = key[:-7] + ".base_layer.weight"
            elif key.endswith(".bias"):
                key = key[:-5] + ".base_layer.bias"
            return key

        for key, value in text_encoder_1_sd.items():
            key = remap_key(key, fused_te_state_dict)
            self.assertTrue(torch.allclose(fused_te_state_dict[key], value))

        for key, value in text_encoder_2_sd.items():
            key = remap_key(key, fused_te_2_state_dict)
            self.assertTrue(torch.allclose(fused_te_2_state_dict[key], value))

        for key, value in unet_state_dict.items():
            self.assertTrue(torch.allclose(unet_state_dict[key], value))

        pipe.fuse_lora()
        pipe.unload_lora_weights()

        assert not state_dicts_almost_equal(text_encoder_1_sd, pipe.text_encoder.state_dict())
        assert not state_dicts_almost_equal(text_encoder_2_sd, pipe.text_encoder_2.state_dict())
        assert not state_dicts_almost_equal(unet_sd, pipe.unet.state_dict())
        release_memory(pipe)
        del unet_sd, text_encoder_1_sd, text_encoder_2_sd

    def test_sdxl_1_0_lora_with_sequential_cpu_offloading(self):
        generator = torch.Generator().manual_seed(0)

        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0")
        pipe.enable_sequential_cpu_offload()
        lora_model_id = "hf-internal-testing/sdxl-1.0-lora"
        lora_filename = "sd_xl_offset_example-lora_1.0.safetensors"

        pipe.load_lora_weights(lora_model_id, weight_name=lora_filename)

        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images

        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.4468, 0.4087, 0.4134, 0.366, 0.3202, 0.3505, 0.3786, 0.387, 0.3535])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipe)

    def test_sd_load_civitai_empty_network_alpha(self):
        """
        This test simply checks that loading a LoRA with an empty network alpha works fine
        See: https://github.com/huggingface/diffusers/issues/5606
        """
        pipeline = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5").to("cuda")
        pipeline.enable_sequential_cpu_offload()
        civitai_path = hf_hub_download("ybelkada/test-ahi-civitai", "ahi_lora_weights.safetensors")
        pipeline.load_lora_weights(civitai_path, adapter_name="ahri")

        images = pipeline(
            "ahri, masterpiece, league of legends",
            output_type="np",
            generator=torch.manual_seed(156),
            num_inference_steps=5,
        ).images
        images = images[0, -3:, -3:, -1].flatten()
        expected = np.array([0.0, 0.0, 0.0, 0.002557, 0.020954, 0.001792, 0.006581, 0.00591, 0.002995])

        self.assertTrue(np.allclose(images, expected, atol=1e-3))
        release_memory(pipeline)

    def test_controlnet_canny_lora(self):
        controlnet = ControlNetModel.from_pretrained("diffusers/controlnet-canny-sdxl-1.0")

        pipe = StableDiffusionXLControlNetPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0", controlnet=controlnet
        )
        pipe.load_lora_weights("nerijs/pixel-art-xl", weight_name="pixel-art-xl.safetensors")
        pipe.enable_sequential_cpu_offload()

        generator = torch.Generator(device="cpu").manual_seed(0)
        prompt = "corgi"
        image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/sd_controlnet/bird_canny.png"
        )

        images = pipe(prompt, image=image, generator=generator, output_type="np", num_inference_steps=3).images

        assert images[0].shape == (768, 512, 3)

        original_image = images[0, -3:, -3:, -1].flatten()
        expected_image = np.array([0.4574, 0.4461, 0.4435, 0.4462, 0.4396, 0.439, 0.4474, 0.4486, 0.4333])
        assert np.allclose(original_image, expected_image, atol=1e-04)
        release_memory(pipe)

    def test_sdxl_t2i_adapter_canny_lora(self):
        adapter = T2IAdapter.from_pretrained("TencentARC/t2i-adapter-lineart-sdxl-1.0", torch_dtype=torch.float16).to(
            "cpu"
        )
        pipe = StableDiffusionXLAdapterPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            adapter=adapter,
            torch_dtype=torch.float16,
            variant="fp16",
        )
        pipe.load_lora_weights("CiroN2022/toy-face", weight_name="toy_face_sdxl.safetensors")
        pipe.enable_model_cpu_offload()
        pipe.set_progress_bar_config(disable=None)

        generator = torch.Generator(device="cpu").manual_seed(0)
        prompt = "toy"
        image = load_image(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/t2i_adapter/toy_canny.png"
        )

        images = pipe(prompt, image=image, generator=generator, output_type="np", num_inference_steps=3).images

        assert images[0].shape == (768, 512, 3)

        image_slice = images[0, -3:, -3:, -1].flatten()
        expected_slice = np.array([0.4284, 0.4337, 0.4319, 0.4255, 0.4329, 0.4280, 0.4338, 0.4420, 0.4226])
        assert numpy_cosine_similarity_distance(image_slice, expected_slice) < 1e-4

    @nightly
    def test_sequential_fuse_unfuse(self):
        pipe = DiffusionPipeline.from_pretrained("stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16)

        # 1. round
        pipe.load_lora_weights("Pclanglais/TintinIA", torch_dtype=torch.float16)
        pipe.to("cuda")
        pipe.fuse_lora()

        generator = torch.Generator().manual_seed(0)
        images = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images
        image_slice = images[0, -3:, -3:, -1].flatten()

        pipe.unfuse_lora()

        # 2. round
        pipe.load_lora_weights("ProomptEngineer/pe-balloon-diffusion-style", torch_dtype=torch.float16)
        pipe.fuse_lora()
        pipe.unfuse_lora()

        # 3. round
        pipe.load_lora_weights("ostris/crayon_style_lora_sdxl", torch_dtype=torch.float16)
        pipe.fuse_lora()
        pipe.unfuse_lora()

        # 4. back to 1st round
        pipe.load_lora_weights("Pclanglais/TintinIA", torch_dtype=torch.float16)
        pipe.fuse_lora()

        generator = torch.Generator().manual_seed(0)
        images_2 = pipe(
            "masterpiece, best quality, mountain", output_type="np", generator=generator, num_inference_steps=2
        ).images
        image_slice_2 = images_2[0, -3:, -3:, -1].flatten()

        self.assertTrue(np.allclose(image_slice, image_slice_2, atol=1e-3))
        release_memory(pipe)
