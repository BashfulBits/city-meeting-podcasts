- Added `torchvision` to the `decision-gpu` lane (locked at 0.29.2) so Qwen3.5-based Karpo models,
  whose image processor imports it, load in the production decision worker image.
