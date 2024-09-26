#!/bin/bash

# Script Name: deploy.sh
# Description: This script deploys (or redeploys) Ray model deployments on a list of specified machines.
# Author: Michael Ripa
# Date: 2024-09-10
# Only works on baulab machines 
##################### CONFIG ######################################################################

MACHINES=("hamada" "fukuyama") # List of machines/ip's (for ssh)
USER="ndif" # Username (for ssh)
REPOS="ndif nnsight"  # Git repos to pull updates from
START_SCRIPT_DIR="~/ndif-deployment" # Path to directory containing env.sh, start.sh and download.py
CONDA_ENV="prod" # Name of conda environment

##################### SCRIPT #######################################################################

for MACHINE in "${MACHINES[@]}"; do
  echo "Connecting to $MACHINE as $USER..."
  
  # Run the following commands on the remote machine
  ssh $USER@$MACHINE << EOF
    echo "Attaching to tmux session on $MACHINE..."
   
    # Activate the specific conda environment

    cd $START_SCRIPT_DIR

    # Navigate to each repo, pull updates
    START_SCRIPT_DIR="$START_SCRIPT_DIR"
    REPOS="$REPOS"
    for REPO in \$REPOS; do
        echo "Updating repository: \$REPO..."
        cd \$REPO
        git pull
        cd ../
    done

    source ~/miniconda3/etc/profile.d/conda.sh
    conda activate $CONDA_ENV

    # Stop the running Ray node (press Ctrl+C)
    echo "Stopping current Ray node..."

    ray stop

    # Start the node
    cd \$START_SCRIPT_DIR
    bash start.sh

    echo "Tasks completed on $MACHINE. Disconnecting..."

EOF
done

echo "All tasks completed."
