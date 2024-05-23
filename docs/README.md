# Unofficial Implementation of ReplaceAnything

If you find this repository helpful, please consider giving us a star⭐!

We only train on a toy datasets for debug, and it is difficult to achieve official results under the condition of insufficient data scale and quality. Because of the consideration of time and cost, we do not intend to collect and filter a large number of high-quality data. If someone has a robust model trained on a large amount of high-quality data and is willing to share it, make a pull request.

**If anyone is willing to provide high-quality data, do not hesitate to contact me.**
## Overview
This repository contains an simple and unofficial implementation of [ReplaceAnything]([https://humanaigc.github.io/animate-anyone/](https://aigcdesigngroup.github.io/replace-anything/)). This project is built upon [Diffusers](https://github.com/huggingface/diffusers) and [BrushNet](https://github.com/TencentARC/BrushNet). This implementation is developed by [Dongxu Yue](https://github.com/dongxuyue).


## 🚀 Getting Started

### Environment Requirement 🌍

BrushNet has been implemented and tested on Pytorch 1.12.1 with python 3.9.

Clone the repo:

```
git clone https://github.com/dongxuyue/Open-ReplaceAnything.git
```

We recommend you first use `conda` to create virtual environment, and install `pytorch` following [official instructions](https://pytorch.org/). For example:


```
conda create -n diffusers python=3.9 -y
conda activate diffusers
python -m pip install --upgrade pip
pip install torch==1.12.1+cu116 torchvision==0.13.1+cu116 torchaudio==0.12.1 --extra-index-url https://download.pytorch.org/whl/cu116
```

Then, you can install diffusers (implemented in this repo) with:

```
pip install -e .
```

After that, you can install required packages thourgh:

```
cd examples/replace_anything/
pip install -r requirements.txt
```

## 🏃🏼 Training
### Stage 1
Stage 1 involves fine-tuning a basic Stable Diffusion U-Net to enhance its character generation capabilities and aesthetics. You can use existing models on [Civitai](https://civitai.com/models/4201?modelVersionId=501240) instead of training from scratch. You can train Stage 1 with:
```
cd examples/replace_anything/
bash train_stage_1.sh
```
You can replace the pretrained SD checkpoints with your own in `train_stage_1.sh` and modify the data path.


### Stage 2
Stage 2 involves training two conditional branches to control the base model trained in Stage 1. Note that you will need paired data (image, text, pose, canny, object) for this training. You can train Stage 2 with:
```
cd examples/replace_anything/
bash train_stage_2.sh
```



## ToDo
- [x] **Release Training Code.**
- [ ] **Release Inference Code.** 
- [ ] **Release Unofficial Pre-trained Weights.**
- [ ] **Release Gradio Demo.**




## Acknowledgements
Special thanks to the original authors of the [ReplaceAnything](https://aigcdesigngroup.github.io/replace-anything/) project and the for their foundational work that inspired this unofficial implementation.

## Email

If you have any questions, do not hesitate to contact: yuedongxu@stu.pku.edu.cn
