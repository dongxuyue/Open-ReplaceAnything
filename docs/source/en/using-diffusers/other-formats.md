<!--Copyright 2024 The HuggingFace Team. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with
the License. You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
-->

# Load different Stable Diffusion formats

[[open-in-colab]]

Stable Diffusion models are available in different formats depending on the framework they're trained and saved with, and where you download them from. Converting these formats for use in 🤗 Diffusers allows you to use all the features supported by the library, such as [using different schedulers](schedulers) for inference, [building your custom pipeline](write_own_pipeline), and a variety of techniques and methods for [optimizing inference speed](../optimization/opt_overview).

<Tip>

We highly recommend using the `.safetensors` format because it is more secure than traditional pickled files which are vulnerable and can be exploited to execute any code on your machine (learn more in the [Load safetensors](using_safetensors) guide).

</Tip>

This guide will show you how to convert other Stable Diffusion formats to be compatible with 🤗 Diffusers.

## PyTorch .ckpt

The checkpoint - or `.ckpt` - format is commonly used to store and save models. The `.ckpt` file contains the entire model and is typically several GBs in size. While you can load and use a `.ckpt` file directly with the [`~StableDiffusionPipeline.from_single_file`] method, it is generally better to convert the `.ckpt` file to 🤗 Diffusers so both formats are available.

There are two options for converting a `.ckpt` file: use a Space to convert the checkpoint or convert the `.ckpt` file with a script.

### Convert with a Space

The easiest and most convenient way to convert a `.ckpt` file is to use the [SD to Diffusers](https://huggingface.co/spaces/diffusers/sd-to-diffusers) Space. You can follow the instructions on the Space to convert the `.ckpt` file.

This approach works well for basic models, but it may struggle with more customized models. You'll know the Space failed if it returns an empty pull request or error. In this case, you can try converting the `.ckpt` file with a script.

### Convert with a script

🤗 Diffusers provides a [conversion script](https://github.com/huggingface/diffusers/blob/main/scripts/convert_original_stable_diffusion_to_diffusers.py) for converting `.ckpt` files. This approach is more reliable than the Space above.

Before you start, make sure you have a local clone of 🤗 Diffusers to run the script and log in to your Hugging Face account so you can open pull requests and push your converted model to the Hub.

```bash
huggingface-cli login
```

To use the script:

1. Git clone the repository containing the `.ckpt` file you want to convert. For this example, let's convert this [TemporalNet](https://huggingface.co/CiaraRowles/TemporalNet) `.ckpt` file:

```bash
git lfs install
git clone https://huggingface.co/CiaraRowles/TemporalNet
```

2. Open a pull request on the repository where you're converting the checkpoint from:

```bash
cd TemporalNet && git fetch origin refs/pr/13:pr/13
git checkout pr/13
```

3. There are several input arguments to configure in the conversion script, but the most important ones are:

    - `checkpoint_path`: the path to the `.ckpt` file to convert.
    - `original_config_file`: a YAML file defining the configuration of the original architecture. If you can't find this file, try searching for the YAML file in the GitHub repository where you found the `.ckpt` file.
    - `dump_path`: the path to the converted model.

        For example, you can take the `cldm_v15.yaml` file from the [ControlNet](https://github.com/lllyasviel/ControlNet/tree/main/models) repository because the TemporalNet model is a Stable Diffusion v1.5 and ControlNet model.

4. Now you can run the script to convert the `.ckpt` file:

```bash
python ../diffusers/scripts/convert_original_stable_diffusion_to_diffusers.py --checkpoint_path temporalnetv3.ckpt --original_config_file cldm_v15.yaml --dump_path ./ --controlnet
```

5. Once the conversion is done, upload your converted model and test out the resulting [pull request](https://huggingface.co/CiaraRowles/TemporalNet/discussions/13)!

```bash
git push origin pr/13:refs/pr/13
```

## Keras .pb or .h5

<Tip warning={true}>

🧪 This is an experimental feature. Only Stable Diffusion v1 checkpoints are supported by the Convert KerasCV Space at the moment.

</Tip>

[KerasCV](https://keras.io/keras_cv/) supports training for [Stable Diffusion](https://github.com/keras-team/keras-cv/blob/master/keras_cv/models/stable_diffusion) v1 and v2. However, it offers limited support for experimenting with Stable Diffusion models for inference and deployment whereas 🤗 Diffusers has a more complete set of features for this purpose, such as different [noise schedulers](https://huggingface.co/docs/diffusers/using-diffusers/schedulers), [flash attention](https://huggingface.co/docs/diffusers/optimization/xformers), and [other
optimization techniques](https://huggingface.co/docs/diffusers/optimization/fp16).

The [Convert KerasCV](https://huggingface.co/spaces/sayakpaul/convert-kerascv-sd-diffusers) Space converts `.pb` or `.h5` files to PyTorch, and then wraps them in a [`StableDiffusionPipeline`] so it is ready for inference. The converted checkpoint is stored in a repository on the Hugging Face Hub.

For this example, let's convert the [`sayakpaul/textual-inversion-kerasio`](https://huggingface.co/sayakpaul/textual-inversion-kerasio/tree/main) checkpoint which was trained with Textual Inversion. It uses the special token `<my-funny-cat>` to personalize images with cats.

The Convert KerasCV Space allows you to input the following:

* Your Hugging Face token.
* Paths to download the UNet and text encoder weights from. Depending on how the model was trained, you don't necessarily need to provide the paths to both the UNet and text encoder. For example, Textual Inversion only requires the embeddings from the text encoder and a text-to-image model only requires the UNet weights.
* Placeholder token is only applicable for textual inversion models.
* The `output_repo_prefix` is the name of the repository where the converted model is stored.

Click the **Submit** button to automatically convert the KerasCV checkpoint! Once the checkpoint is successfully converted, you'll see a link to the new repository containing the converted checkpoint. Follow the link to the new repository, and you'll see the Convert KerasCV Space generated a model card with an inference widget to try out the converted model.

If you prefer to run inference with code, click on the **Use in Diffusers** button in the upper right corner of the model card to copy and paste the code snippet:

```py
from diffusers import DiffusionPipeline

pipeline = DiffusionPipeline.from_pretrained(
    "sayakpaul/textual-inversion-cat-kerascv_sd_diffusers_pipeline", use_safetensors=True
)
```

Then, you can generate an image like:

```py
from diffusers import DiffusionPipeline

pipeline = DiffusionPipeline.from_pretrained(
    "sayakpaul/textual-inversion-cat-kerascv_sd_diffusers_pipeline", use_safetensors=True
)
pipeline.to("cuda")

placeholder_token = "<my-funny-cat-token>"
prompt = f"two {placeholder_token} getting married, photorealistic, high quality"
image = pipeline(prompt, num_inference_steps=50).images[0]
```

## A1111 LoRA files

[Automatic1111](https://github.com/AUTOMATIC1111/stable-diffusion-webui) (A1111) is a popular web UI for Stable Diffusion that supports model sharing platforms like [Civitai](https://civitai.com/). Models trained with the Low-Rank Adaptation (LoRA) technique are especially popular because they're fast to train and have a much smaller file size than a fully finetuned model. 🤗 Diffusers supports loading A1111 LoRA checkpoints with [`~loaders.LoraLoaderMixin.load_lora_weights`]:

```py
from diffusers import StableDiffusionXLPipeline
import torch

pipeline = StableDiffusionXLPipeline.from_pretrained(
    "Lykon/dreamshaper-xl-1-0", torch_dtype=torch.float16, variant="fp16"
).to("cuda")
```

Download a LoRA checkpoint from Civitai; this example uses the [Blueprintify SD XL 1.0](https://civitai.com/models/150986/blueprintify-sd-xl-10) checkpoint, but feel free to try out any LoRA checkpoint!

```py
# uncomment to download the safetensor weights
#!wget https://civitai.com/api/download/models/168776 -O blueprintify.safetensors
```

Load the LoRA checkpoint into the pipeline with the [`~loaders.LoraLoaderMixin.load_lora_weights`] method:

```py
pipeline.load_lora_weights(".", weight_name="blueprintify.safetensors")
```

Now you can use the pipeline to generate images:

```py
prompt = "bl3uprint, a highly detailed blueprint of the empire state building, explaining how to build all parts, many txt, blueprint grid backdrop"
negative_prompt = "lowres, cropped, worst quality, low quality, normal quality, artifacts, signature, watermark, username, blurry, more than one bridge, bad architecture"

image = pipeline(
    prompt=prompt,
    negative_prompt=negative_prompt,
    generator=torch.manual_seed(0),
).images[0]
image
```

<div class="flex justify-center">
    <img src="https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/diffusers/blueprint-lora.png"/>
</div>
