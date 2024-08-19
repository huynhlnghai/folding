#!/bin/bash

# Activate the virtual environment
source .venv/bin/activate

# Execute the Python script
python3 ./neurons/miner.py \
    --netuid 25 \
    --subtensor.network finney \
    --wallet.name <your_coldkey> \
    --wallet.hotkey <your_hotkey> \
    --neuron.max_workers <number of processes to run on your machine> \
    --wandb.off true \
    --neuron.device gpu \
    --axon.port <your_port>