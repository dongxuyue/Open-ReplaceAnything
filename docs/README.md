# Unofficial Implementation of ReplaceAnything

If you find this repository helpful, please consider giving us a star⭐!

We only train on a toy datasets for debug, and it is difficult to achieve official results under the condition of insufficient data scale and quality. Because of the consideration of time and cost, we do not intend to collect and filter a large number of high-quality data. If someone has a robust model trained on a large amount of high-quality data and is willing to share it, make a pull request.

**If anyone is willing to provide high-quality data, do not hesitate to contact me.**
https://github.com/TencentARC/BrushNet
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


## Sample of Result on UBC-fashion dataset
### Stage 1
The current version of the face still has some artifacts.  This model is trained on the UBC dataset rather than a large-scale dataset.
<table class="center">
    <tr><td><img src="./assets/stage1/1.png"></td><td><img src="./assets/stage1/2.png"></td></tr>
    <tr><td><img src="./assets/stage1/3.png"></td><td><img src="./assets/stage1/8.png"></td></tr>
    <tr><td><img src="./assets/stage1/9.png"></td><td><img src="./assets/stage1/10.png"></td></tr>
    <tr><td><img src="./assets/stage1/4.png"></td><td><img src="./assets/stage1/5.png"></td></tr>
    <tr><td><img src="./assets/stage1/6.png"></td><td><img src="./assets/stage1/7.png"></td></tr>

</table>
<p style="margin-left: 2em; margin-top: -1em"></p>

### Stage 2
The training of stage2 is challenging due to artifacts in the background. We select one of our best results here, and are still working on it. An important point is to ensure that training and inference resolution is consistent.
<table class="center">
    <tr><td><img src="./assets/stage2/1.gif"></td></tr>

</table>
<p style="margin-left: 2em; margin-top: -1em"></p>

## ToDo
- [x] **Release Training Code.**
- [x] **Release Inference Code.** 
- [ ] **Release Unofficial Pre-trained Weights.**
- [x] **Release Gradio Demo.**

## Requirements

```bash
bash fast_env.sh
```

## 🎬Gradio Demo
```python
python3 -m demo.gradio_animate
```
For a 13-second pose video, processing at 256 resolution requires 11G VRAM, and at 512 resolution, it requires 23.5G VRAM.

## Training
### Original AnimateAnyone Architecture (It is difficult to control pose when training on a small dataset.)
#### First Stage

```python
torchrun --nnodes=8 --nproc_per_node=8 train.py --config configs/training/train_stage_1.yaml
```

#### Second Stage

```python
torchrun --nnodes=8 --nproc_per_node=8 train.py --config configs/training/train_stage_2.yaml
```

### Our Method (A more dense pose control scheme, the number of parameters is still small.) (Highly recommended)
```python
torchrun --nnodes=8 --nproc_per_node=8 train_hack.py --config configs/training/train_stage_1.yaml
```

#### Second Stage

```python
torchrun --nnodes=8 --nproc_per_node=8 train_hack.py --config configs/training/train_stage_2.yaml
```


## Acknowledgements
Special thanks to the original authors of the [Animate Anyone](https://humanaigc.github.io/animate-anyone/) project and the contributors to the [magic-animate](https://github.com/magic-research/magic-animate/tree/main) and [AnimateDiff](https://github.com/guoyww/AnimateDiff) repository for their open research and foundational work that inspired this unofficial implementation.

## Email

For academic or business cooperation only: guoqin@stu.pku.edu.cn
