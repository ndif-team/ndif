#!/bin/bash

resources=`python -m src.ray.resources`

ray start --resources "$resources"