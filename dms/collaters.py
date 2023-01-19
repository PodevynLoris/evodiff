import numpy as np
import torch
from dms.utils import Tokenizer
from sequence_models.constants import PAD, PROTEIN_ALPHABET, GAP


def _pad(tokenized, value, dim=2):
    """
    Utility function that pads batches to the same length.

    tokenized: list of tokenized sequences
    value: pad index
    """
    batch_size = len(tokenized)
    max_len = max(len(t) for t in tokenized)
    if dim == 3: # dim = 3 (one hot)
        categories = tokenized[0].shape[-1]
        output = torch.zeros((batch_size, max_len, categories)) + value
        for row, t in enumerate(tokenized):
            output[row, :len(t), :] = t
    elif dim == 2: # dim = 2 (tokenized)
        output = torch.zeros((batch_size, max_len)) + value
        for row, t in enumerate(tokenized):
            output[row, :len(t)] = t
    else:
        print("padding not supported for dim > 3")
    return output

def _pad_msa(tokenized, num_seq, max_len, value, dim=3):
    """Utility function that pads batches to the same length."""
    batch_size = len(tokenized)
    if dim == 4: # one hot MSA
        categories = tokenized[0].shape[-1] # last dim is one hot
        output = torch.zeros((batch_size, num_seq, max_len, categories), dtype=torch.long) + value
        for i in range(batch_size):
            output[i, :, :len(tokenized[i][0]), :] = tokenized[i]
    elif dim == 3: # tokenized MSA
        output = torch.zeros((batch_size, num_seq, max_len), dtype=torch.long) + value
        for i in range(batch_size):
            output[i, :, :len(tokenized[i][0])] = tokenized[i]
    else:
        print("padding not supported for dim > 4")
    return output

def _unpad(x, value):
    x_pad = x.clone()
    mask_pad = x_pad != value
    x = x[mask_pad].to(torch.int64)
    return x

def sample_transition_matrix(x_0, Q_bar):
    """
    Sample a markov transition according to next_step = x_0 * q ^ time,
    where Q_bar = q ^t or cumprod of scheduled transition matrices
    returns sample and probabilities
    """
    p_next_step = torch.mm(x_0, Q_bar)
    next_step = torch.multinomial(p_next_step, num_samples=1)
    return next_step.squeeze(), p_next_step # sample and probabilities


class OAMaskCollater(object):
    """
    OrderAgnosic Mask Collater for masking batch data according to Hoogeboom et al. OA ARDMS
    inputs:
        sequences : list of sequences
        inputs_padded: if inputs are padded (due to truncation in Simple_Collater) set True (default False)

    OA-ARM variables:
        D : possible permutations from 0.. max length
        t : randomly selected timestep

    outputs:
        src : source  masked sequences (model input)
        timesteps: (D-t+1) term
        tokenized: tokenized sequences (target seq)
        masks: masks used to generate src
    """
    def __init__(self, tokenizer=Tokenizer()):
        self.tokenizer = tokenizer

    def __call__(self, sequences):
        tokenized = [torch.tensor(self.tokenizer.tokenize(s)) for s in sequences]
        max_len = max(len(t) for t in tokenized)
        src=[]
        timesteps = []
        masks=[]
        mask_id = torch.tensor(self.tokenizer.mask_id, dtype=torch.int64)
        for i,x in enumerate(tokenized):
            # Randomly generate timestep and indices to mask
            D = len(x) # D should have the same dimensions as each sequence length
            if D <= 1:  # for sequence length = 1 in dataset
                t = 1
            else:
                t = np.random.randint(1, D) # randomly sample timestep
            num_mask = (D-t+1) # from OA-ARMS
            # Append timestep
            timesteps.append(num_mask)
            # Generate mask
            mask_arr = np.random.choice(D, num_mask, replace=False) # Generates array of len num_mask
            index_arr = np.arange(0, max_len) #index array [1...seq_len]
            mask = np.isin(index_arr, mask_arr, invert=False).reshape(index_arr.shape) # mask bools indices specified by mask_arr
            # Mask inputs
            mask = torch.tensor(mask, dtype=torch.bool)
            masks.append(mask)
            x_t = ~mask[0:D] * x + mask[0:D] * mask_id
            src.append(x_t)
        # PAD out
        src = _pad(src, self.tokenizer.pad_id)
        masks = _pad(masks*1,0) #, self.seq_length, 0)
        tokenized = _pad(tokenized, self.tokenizer.pad_id)
        return (src.to(torch.long), torch.tensor(timesteps), tokenized.to(torch.long), masks)


class MSAAbsorbingARDMCollater():
    """Collater for MSA Absorbing Diffusion model.
    Based on implementation described by Hoogeboom et al. in "Autoregressive Diffusion Models"
    https://doi.org/10.48550/arXiv.2110.02037
    Parameters:
        alphabet: str,
            protein alphabet to use
        pad_token: str,
            pad_token to use to pad MSAs, default is PAD token from sequence_models.constants
        num_seqs: int,
            number of sequences to include in each MSA
    Input (list): a batch of Multiple Sequence Alignments (MSAs), each MSA contains 64 sequences
    Output:
        src (torch.LongTensor): corrupted input + padding
        tgt (torch.LongTensor): input + padding
        mask (torch.LongTensor): 1 where tgt is not padding
    """

    def __init__(self, alphabet: str, pad_token=PAD, num_seqs=64):
        self.tokenizer = Tokenizer(alphabet)
        self.pad_idx = self.tokenizer.alphabet.index(pad_token)
        self.num_seqs = num_seqs

    def __call__(self, batch_msa):
        tgt = list(batch_msa)
        src = tgt.copy()

        longest_msa = 0
        batch_size = len(batch_msa)

        for i in range(batch_size):
            # Tokenize MSA
            tgt[i] = [torch.tensor(self.tokenizer.tokenizeMSA(s)) for s in tgt[i]]
            src[i] = [self.tokenizer.tokenizeMSA(s) for s in src[i]]

            curr_msa = src[i]

            curr_msa = np.asarray(curr_msa)
            length, depth = curr_msa.shape  # length = number of seqs in MSA, depth = # AA in MSA

            curr_msa = curr_msa.flatten()  # Flatten MSA to 1D to mask tokens
            d = len(curr_msa)  # number of residues in MSA
            t = np.random.choice(d)  # Pick timestep t
            t += 1  # ensure t cannot be 0

            num_masked_tokens = d - t + 1
            mask_idx = np.random.choice(d, num_masked_tokens, replace=False)  # Pick D-t+1 random indices to mask
            curr_msa[mask_idx] = self.tokenizer.mask_id
            curr_msa = curr_msa.reshape(length, depth)
            src[i] = torch.tensor(curr_msa)

            longest_msa = max(depth, longest_msa)  # Keep track of the longest MSA for padding
            tgt[i] = torch.stack(tgt[i])
        # Pad sequences
        src = _pad_msa(src, self.num_seqs, longest_msa, self.pad_idx)
        tgt = _pad_msa(tgt, self.num_seqs, longest_msa, self.pad_idx)
        mask = (src == self.tokenizer.mask_id)
        #print(src.shape, tgt.shape, mask.shape)
        return src, tgt, mask


class D3PMCollater(object):
    """
    D3PM Collater for generating batch data according to markov process according to Austin et al.
    inputs:
        sequences : list of sequences
        tokenizer: Tokenizer()
        masking scheme: 'BLOSUM' uses blosum matrix, 'RANDOM' uses uniform transition matrix
        num_timesteps: number of diffusion timesteps

    outputs:
        src : source  masked sequences (model input)
        timesteps: (D-t+1) term
        tokenized: tokenized sequences (target seq)
        masks: masks used to generate src
        Q : markov matrix
        q_x : forward transition probabilities
    """
    def __init__(self, tokenizer=Tokenizer(), num_timesteps=100, Q=None, Q_bar=None):
        self.tokenizer = tokenizer
        self.num_timesteps = num_timesteps # Only needed for markov trans, doesnt depend on seq len
        self.K = self.tokenizer.K-4
        self.Q = Q
        self.Q_bar =Q_bar

    def __call__(self, sequences):
        # Pre pad one-hot arrays
        pad_one_hot = torch.zeros((self.K))

        tokenized = [torch.tensor(self.tokenizer.tokenize(s)) for s in sequences]
        max_len = max(len(t) for t in tokenized)

        one_hot = pad_one_hot.repeat((len(tokenized), max_len, 1))
        ## This is to deal with an empty sequence ##
        del_index = None
        for i,t in enumerate(tokenized):
            if len(t) == 0: # TODO: was this an old bug? can i delete now -check ignore empty sequence in MNIST dataset
               #one_hot.append(torch.zeros(self.K, dtype=torch.double))
               del_index = i
            else:
                one_hot[i, :len(t), :] = self.tokenizer.one_hot(t)
                #one_hot.append(self.tokenizer.one_hot(t))
        if del_index is not None:
           tokenized.pop(del_index)
           one_hot = torch.cat((one_hot[:del_index], one_hot[del_index + 1:]))
           #one_hot.pop(del_index)
        one_hot = one_hot.to(torch.double)
        src=[]
        timesteps = []
        q_x = pad_one_hot.repeat((len(tokenized), max_len, 1))
        src_one_hot = pad_one_hot.repeat((len(tokenized), max_len, 1))
        for i,t in enumerate(tokenized): # enumerate over batch
            D = len(t)  # sequence length
            x = one_hot[i, :D, :] #self.tokenizer.one_hot(t)
            #one_hot[i,:D,:] = x
            t = np.random.randint(1, self.num_timesteps) # randomly sample timestep
            # Append timestep
            timesteps.append(t)
            # Calculate forward at time t and t-1
            x_t, q_x_t = sample_transition_matrix(x, self.Q_bar[t]) # x = tgt, x_t = src, Q_bar[t] is cum prod @ time t
            #x_tminus1, q_x_tminus1 = sample_transition_matrix(x, self.Q_bar[t-1])
            src.append(x_t)
            src_one_hot[i, :D, :] = self.tokenizer.one_hot(x_t)
            q_x[i, :D, :] = q_x_t
            #q_x_minus1[i, :D, :] = q_x_tminus1
        # PAD out
        #one_hot = torch.stack(one_hot)
        src = _pad(src, self.tokenizer.pad_id)
        tokenized = _pad(tokenized, self.tokenizer.pad_id)
        return (src.to(torch.long), src_one_hot.to(torch.double), torch.tensor(timesteps), tokenized.to(torch.long),
                one_hot.to(torch.double), self.Q, self.Q_bar, q_x.to(torch.double)) #, q_x_minus1.to(torch.double))


class D3PMCollaterMSA(object):
    """
    """
    def __init__(self, tokenizer=Tokenizer(), num_timesteps=100, Q=None, Q_bar=None, num_seqs=64):
        self.tokenizer = tokenizer
        self.num_timesteps = num_timesteps  # Only needed for markov trans, doesnt depend on seq len
        self.K = self.tokenizer.K
        self.Q = Q
        self.Q_bar = Q_bar
        self.num_seqs = num_seqs

    def __call__(self, msas):
        batch_size = len(msas)
        tokenized = list(msas)  # tgt

        # Init arrays to save too
        max_seq_len = max(len(t[0]) for t in tokenized)  # all seqs in MSA are the same len
        timesteps = []
        src = tokenized.copy()
        src_one_hot = tokenized.copy()
        tgt_one_hot = tokenized.copy()
        pad_one_hot = torch.zeros((self.K)) # prepad one hot tensor for saving
        q_x = pad_one_hot.repeat((batch_size, self.num_seqs, max_seq_len, 1))

        for i in range(batch_size):  # enumerate over batch
            tokenized[i] = [torch.tensor(self.tokenizer.tokenizeMSA(s)) for s in msas[i]]
            tgt_one_hot[i] = [self.tokenizer.one_hot(t) for t in tokenized[i]]
            curr_msa = torch.stack(tgt_one_hot[i])
            length, depth, tokens = curr_msa.shape  # length = number of seqs in MSA, depth = # AA in MSA
            curr_msa = curr_msa.flatten(start_dim=0, end_dim=1)
            # Append timestep
            t = np.random.randint(1, self.num_timesteps)  # randomly sample timestep
            timesteps.append(t)
            # Calculate target
            x_t, q_x_t = sample_transition_matrix(curr_msa, self.Q_bar[t])  # x = tgt, x_t = src, Q_bar accounts for time
            # Reshape back to MSA
            x_t = x_t.reshape(length, depth)
            q_x_t = q_x_t.reshape(length, depth, tokens)
            src[i] = x_t
            src_one_hot[i] = [self.tokenizer.one_hot(t) for t in x_t]
            q_x[i, :, :depth, :] = q_x_t
            tokenized[i] = torch.stack(tokenized[i])  #replace list with stack
            src_one_hot[i] = torch.stack(src_one_hot[i])
            tgt_one_hot[i] = torch.stack(tgt_one_hot[i])
        # PAD out
        src = _pad_msa(src, self.num_seqs, max_seq_len, self.tokenizer.pad_id)
        tokenized = _pad_msa(tokenized, self.num_seqs, max_seq_len, self.tokenizer.pad_id)
        src_one_hot = _pad_msa(src_one_hot, self.num_seqs, max_seq_len, self.tokenizer.pad_id, dim=4)
        tgt_one_hot = _pad_msa(tgt_one_hot, self.num_seqs, max_seq_len, self.tokenizer.pad_id, dim=4)
        return (src.to(torch.long), src_one_hot.to(torch.double), torch.tensor(timesteps), tokenized.to(torch.long),
                tgt_one_hot.to(torch.double), self.Q, self.Q_bar, q_x.to(torch.double))