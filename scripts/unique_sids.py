import re


def main(file_path):

    with open(file_path, 'r') as file:

        log_text = file.read()

    regex = r'Responding to SID: `(.+)`:'


    matches = re.findall(regex, log_text)

    breakpoint()



if __name__ == '__main__':

    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument('file_path')

    main(**vars(parser.parse_args()))