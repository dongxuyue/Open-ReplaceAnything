export MODEL_NAME="runwayml/stable-diffusion-v1-5"

accelerate launch --mixed_precision="fp16"  train_replacenet_stage_1.py \
  --pretrained_model_name_or_path=$MODEL_NAME \
  --resolution=512 \
  --train_batch_size=1 \
  --gradient_checkpointing \
  --max_train_steps=15000 \
  --learning_rate=1e-05 \
  --data_root_path="./training_data"  \
  --output_dir="./replacenet_output_stage_1" \
