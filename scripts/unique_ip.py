import re
from glob import glob
import pandas as pd

def get_ips(file_path):
        

    with open(file_path, 'r') as file:

        log_text = file.read()
        
    regex = r'(\d+\.\d+\.\d+\.\d+).+\[(\d+\/\w+\/\d{4}).+\].+GET \/result\/'

    matches = re.findall(regex, log_text)
    
    return matches
    

def main(path, unique):
    
    ips = []
    
    for file in glob(f"{path}/*"):
        
        ips.extend(get_ips(file))

    data = pd.DataFrame(ips, columns=["ip", "date"])
    
    breakpoint()
    
    if unique:
    
        data = data.drop_duplicates(subset=["ip", "date"])
        
    ax = data.sort_values('date', axis=0)['date'].value_counts(sort=False).plot(kind='bar',
                                    figsize=(14,10),
                                    title="# requests"
                                    )
    ax.set_xlabel("Date", rotation=45)
    ax.set_ylabel("# requests")

    ax.figure.savefig('requests.png')
    
    



if __name__ == '__main__':

    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument('path')
    parser.add_argument('--unique', action="store_true", default=False)

    main(**vars(parser.parse_args()))