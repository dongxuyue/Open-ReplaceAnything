export MODEL_NAME="/cto_studio/vistring/yuedongxu/pretrained/sd_pretrained/sd_15"

accelerate launch --mixed_precision="fp16"  train_replacenet_stage_1.py \
  --pretrained_model_name_or_path=$MODEL_NAME \
  --resolution=512 \
  --train_batch_size=1 \
  --gradient_checkpointing \
  --max_train_steps=15000 \
  --learning_rate=1e-05 \
  --output_dir="/cto_studio/vistring/yuedongxu/BrushNet/examples/brushnet/replacenet_output" \
