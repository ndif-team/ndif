#!/bin/bash

resources=`python -m src.ray.resources --head`

ray start --head --block --resources "$resources"

# serve deploy src/ray/config/ray_config.yml

# tail -f /dev/null