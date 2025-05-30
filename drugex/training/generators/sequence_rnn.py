import pandas as pd

import torch
from torch import nn
from torch import optim
from tqdm.auto import tqdm

from drugex import DEFAULT_DEVICE, DEFAULT_GPUS
from drugex.training.generators.interfaces import Generator


class SequenceRNN(Generator):
    """
    Sequence RNN model for molecule generation.
    """
    def __init__(self, voc, embed_size=128, hidden_size=512, is_lstm=True, lr=1e-3, device=DEFAULT_DEVICE, use_gpus=DEFAULT_GPUS):
        super(SequenceRNN, self).__init__(device=device, use_gpus=use_gpus)
        self.voc = voc
        self.embed_size = embed_size
        self.hidden_size = hidden_size
        self.output_size = voc.size

        self.embed = nn.Embedding(voc.size, embed_size)
        self.is_lstm = is_lstm
        rnn_layer = nn.LSTM if is_lstm else nn.GRU
        self.rnn = rnn_layer(embed_size, hidden_size, num_layers=3, batch_first=True)
        self.linear = nn.Linear(hidden_size, voc.size)
        self.optim = optim.Adam(self.parameters(), lr=lr)
        self.attachToGPUs(self.gpus)

        self.model_name = 'SequenceRNN'

    def attachToGPUs(self, gpus):
        """
        This model currently uses only one GPU. Therefore, only the first one from the list will be used.

        Parameters:
        ----------
        gpus: `tuple`
            A tuple of GPU indices.
        
        Returns:
        -------
        None
        """
        if gpus and gpus[0] >= 0:
            self.device = torch.device(f'cuda:{gpus[0]}')
        else:
            self.device = torch.device('cpu')
        self.to(self.device)
        self.gpus = (gpus[0],) if gpus else ()


    def forward(self, input, h):
        """
        Forward pass of the model.
        
        Parameters:
        ----------
        input: `torch.Tensor`
            Input tensor of shape (batch_size, 1).
        h: `torch.Tensor`
            # TODO: Verify h shape.
            Hidden state tensor of shape (num_layers, batch_size, hidden_size).
        
        Returns:
        -------
        TODO: fill outputs
        """
        output = self.embed(input.unsqueeze(-1))
        output, h_out = self.rnn(output, h)
        output = self.linear(output).squeeze(1)
        return output, h_out

    def init_h(self, batch_size, labels=None):
        """
        Initialize hidden state of the model.

        Hidden state is initialized with random values.
        If labels are provided, the first hidden state will be set to the labels.
        
        Parameters:
        ----------
        batch_size: `int`
            Batch size.
        labels: `torch.Tensor`
            Labels tensor of shape (batch_size, 1).
            
        Returns:
        -------
        TODO: fill outputs
        """

        h = torch.rand(3, batch_size, 512).to(self.device)
        if labels is not None:
            h[0, batch_size, 0] = labels
        if self.is_lstm:
            c = torch.rand(3, batch_size, self.hidden_size).to(self.device)
        return (h, c) if self.is_lstm else h

    def likelihood(self, target):
        """
        Calculate the likelihood of the target sequence.
        
        Parameters:
        ----------
        target: `torch.Tensor`
            Target tensor of shape (batch_size, seq_len).
        
        Returns:
        -------
        scores: `torch.Tensor`
            Scores tensor of shape (batch_size, seq_len).
        """

        batch_size, seq_len = target.size()
        x = torch.LongTensor([self.voc.tk2ix['GO']] * batch_size).to(self.device)
        h = self.init_h(batch_size)
        scores = torch.zeros(batch_size, seq_len).to(self.device)
        for step in range(seq_len):
            logits, h = self(x, h)
            logits = logits.log_softmax(dim=-1)
            score = logits.gather(1, target[:, step:step+1]).squeeze()
            scores[:, step] = score
            x = target[:, step]
        return scores
    
    def sample(self, batch_size):
        """
        Sample a SMILES from the model.
        
        Parameters:
        ----------
        batch_size: `int`
            Batch size.
            
        Returns:
        -------
        smiles: `list`
            List of SMILES.
        """
        x = torch.LongTensor([self.voc.tk2ix['GO']] * batch_size).to(self.device)
        h = self.init_h(batch_size)
        sequences = torch.zeros(batch_size, self.voc.max_len).long().to(self.device)
        isEnd = torch.zeros(batch_size).bool().to(self.device)

        for step in range(self.voc.max_len):
            logit, h = self(x, h)
            proba = logit.softmax(dim=-1)
            x = torch.multinomial(proba, 1).view(-1)
            x[isEnd] = self.voc.tk2ix['EOS']
            sequences[:, step] = x
            end_token = (x == self.voc.tk2ix['EOS'])
            isEnd = torch.ge(isEnd + end_token, 1)
            if (isEnd == 1).all(): break

        return [self.voc.decode(s, is_tk = False) for s in sequences]

    def evolve(self, batch_size, epsilon=0.01, crover=None, mutate=None):
        """
        Evolve a SMILES from the model by sequantial addition of tokens.
        
        Parameters:
        ----------
        batch_size: `int`
            Batch size.
        epsilon: `float`
            Probability using the mutate network to generate the next token.
        crover: `drugex.models.Crover`
            Crover network.
        mutate: `drugex.models.Mutate`
            Mutate network.
        
        Returns:
        -------
        TODO: check if ouput smiles are still encoded"""
        
        # Start tokens
        x = torch.LongTensor([self.voc.tk2ix['GO']] * batch_size).to(self.device)
        # Hidden states initialization for exploitation network (agent)
        hA = self.init_h(batch_size)
        # Hidden states initialization for exploration networks (mutate and crover)
        hM = self.init_h(batch_size)
        hC = self.init_h(batch_size)
        # Initialization of output matrix
        sequences = torch.zeros(batch_size, self.voc.max_len).long().to(self.device)
        # labels to judge and record which sample is ended
        is_end = torch.zeros(batch_size).bool().to(self.device)

        for step in range(self.voc.max_len):
            # Get unscaled logits and hidden states from agent network and convert them to probabilities with softmax
            logitA, hA = self(x, hA)
            proba = logitA.softmax(dim=-1)

            # If crover combine probablities from agent network and crover network
            if crover is not None:
                ratio = torch.rand(batch_size, 1).to(self.device)
                logitC, hC = crover(x, hC)
                proba = proba * ratio + logitC.softmax(dim=-1) * (1 - ratio)
            
            # If mutate replace with the epsilon-rate the probabilities with the ones from the mutation network
            if mutate is not None:
                logitM, hM = mutate(x, hM)
                is_mutate = (torch.rand(batch_size) < epsilon).to(self.device)
                proba[is_mutate, :] = logitM.softmax(dim=-1)[is_mutate, :]

            # sampling based on output probability distribution
            x = torch.multinomial(proba, 1).view(-1)

            is_end |= x == self.voc.tk2ix['EOS']
            x[is_end] = self.voc.tk2ix['EOS']
            sequences[:, step] = x
            if is_end.all(): break
        return sequences

    def trainNet(self, loader, epoch, epochs):

        """
        Train the RNN network for one epoch

        Parameters:
        -----------
        loader : torch.utils.data.DataLoader
            The data loader for the training set
        epoch : int
            The current epoch
        epochs : int
            The total number of epochs
            
        Returns
        -------
        loss : float
            The training loss of the epoch
        """

        total_steps = len(loader)
        for i, batch in enumerate(loader):
            self.optim.zero_grad()
            loss = self.likelihood(batch.to(self.device))
            loss = -loss.mean()
            loss.backward()
            self.optim.step()
            self.monitor.saveProgress(self, i, epoch, total_steps, epochs, loss.item())

        return loss.item()

    def validateNet(self, loader=None, evaluator=None, no_multifrag_smiles=True, n_samples=128):

        """
        Validate the network
        
        Parameters
        ----------
        loader : torch.utils.data.DataLoader
            A dataloader object to iterate over the validation data to compute the validation loss
        evaluator : Evaluator
            An evaluator object to evaluate the generated SMILES
        no_multifrag_smiles : bool
            If `True`, only single-fragment SMILES are considered valid
        n_samples : int
            The number of SMILES to sample from the model
        
        Returns
        -------
        valid_metrics : dict
            Dictionary containing the validation metrics
        scores : pandas.DataFrame
            DataFrame containing Smiles, frags and the scores for each SMILES    

        Notes
        -----
        The validation metrics are:
            - valid_ratio: the ratio of valid SMILES
            - accurate_ratio: the ratio of SMILES that are valid and have the desired fragments
            - loss_valid: the validation loss
        """

        valid_metrics = {}
        smiles = self.sample(n_samples)
        scores = self.evaluate(smiles, evaluator=evaluator)
        scores['SMILES'] = smiles
        valid_metrics['valid_ratio'] = scores.Valid.mean()

        # If a separate validation set is provided, use it to compute the validation loss 
        if loader is not None:
            loss_valid, size = 0, 0
            for j, batch in enumerate(loader):
                size += batch.size(0)
                loss_valid += -self.likelihood(batch.to(self.device)).sum().item()
            valid_metrics['loss_valid'] = loss_valid / size / self.voc.max_len

        return valid_metrics, scores       

    def generate(self, num_samples=100, batch_size=32, n_proc=1,
                drop_duplicates=True, drop_invalid=True, 
                evaluator=None, no_multifrag_smiles=True, drop_undesired=False, raw_scores=True,
                progress=True, tqdm_kwargs={}):
        
        if progress:
            tqdm_kwargs.update({'total': num_samples, 'desc': 'Generating molecules'})
            pbar = tqdm(**tqdm_kwargs)

        df_all = pd.DataFrame(columns=['SMILES', 'Frags']) # Frags column to be compatible with other generators
        while not len(df_all) >= num_samples:
            with torch.no_grad():
                new_smiles = self.sample(batch_size)   
                df_new = pd.DataFrame({'SMILES': new_smiles, 'Frags': None}) 

                # If drop_invalid is True, invalid (and inaccurate) SMILES are dropped
                # valid molecules are canonicalized and optionally extra filtering is applied
                # else invalid molecules are kept and no filtering is applied
                if drop_invalid:
                    df_new = self.filterNewMolecules(df_all, df_new, with_frags=False, drop_duplicates=drop_duplicates, drop_undesired=drop_undesired, evaluator=evaluator, no_multifrag_smiles=no_multifrag_smiles)

                # Update the dataframe with the new molecules
                df_all = pd.concat([df_all, df_new], axis=0, ignore_index=True)
                
                # Update progress bar
                if progress:
                    pbar.update(len(df_new) if pbar.n + len(df_new) <= num_samples else num_samples - pbar.n)
        
        if progress:
            pbar.close()


        # Post-processing
        df = df_all.head(num_samples)
        if evaluator:
            df = pd.concat([df, self.evaluate(df.SMILES.tolist(), evaluator=evaluator, no_multifrag_smiles=no_multifrag_smiles, unmodified_scores=raw_scores)[evaluator.getScorerKeys()]], axis=1)    
        return df.drop('Frags', axis=1).round(decimals=3)