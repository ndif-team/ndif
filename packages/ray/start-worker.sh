#!/bin/bash

if [ -z "$resource_name" ]; then
    resources=`python -m src.ray.resources`
else
    resources=`python -m src.ray.resources --name $resource_name`
fi

ray start --resources "$resources" --address $RAY_ADDRESS

tail -f /dev/null
