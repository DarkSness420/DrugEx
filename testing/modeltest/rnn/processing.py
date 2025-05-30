import pandas as pd

from drugex.data.corpus.corpus import SequenceCorpus
from drugex.data.corpus.vocabulary import VocSmiles
from drugex.data.datasets import SmilesDataSet
from drugex.data.processing import Standardization, CorpusEncoder, RandomTrainTestSplitter
from ..settings import *

def main():
    if not os.path.exists(f'{OUTPUT_FILE}_train.tsv'):
        data_set = SmilesDataSet(f'{OUTPUT_FILE}.tsv', rewrite=True)
        voc = VocSmiles(False)
        print('Loading data...')
        smiles = pd.read_csv(INPUT_FILE, header=0, sep='\t', usecols=['Smiles']).squeeze('columns').tolist()
        print(len(smiles))

        print('Standardizing...')
        standardizer = Standardization(n_proc=N_PROC, chunk_size=CHUNK_SIZE)
        smiles = standardizer.apply(smiles)

        print('Encoding...')
        encoder = CorpusEncoder(
            SequenceCorpus,
            {
                'vocabulary': voc,
                'update_voc': False,
                'throw': True

            },
            n_proc=N_PROC,
            chunk_size=CHUNK_SIZE
        )

        encoder.apply(smiles, collector=data_set)
        splitter = RandomTrainTestSplitter(0.1, 1e4)
        train, test = splitter(data_set.getData())
        for df, split in zip([train, test], ['train', 'test']):
            pd.DataFrame(df, columns=data_set.getColumns()).to_csv(f'{OUTPUT_FILE}_{split}.tsv', header=True, index=False, sep='\t')

        print('Preprocessing done.')

if __name__ == '__main__':
    main()
