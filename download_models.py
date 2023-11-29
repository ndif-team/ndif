from huggingface_hub import snapshot_download

from . import CONFIG

# For each model configuration, download the needed files if they don't exist.
# Set checkpoint path of each model.
# TODO not sure if we can only pattern match certain files like .model .bin or .safetensors to not waste space. did not work with gptj
# TODO maybe pre downloading things isnt even something we should do
for model_configuration in CONFIG.MODEL_CONFIGURATIONS:
    model_configuration.checkpoint_path = snapshot_download(
        model_configuration.repo_id,
        force_download=False
                )

