import importlib


if __name__ == "__main__":
    
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument("controller_class")
    
    args = parser.parse_args()
    
    controller_class = args.controller_class

    module = importlib.import_module(controller_class)
    
    module.app()
