#!/bin/bash

resources=`python -m src.ray.resources --head`

ray start --head --resources "$resources"

serve deploy src/ray/config/ray_config.yml