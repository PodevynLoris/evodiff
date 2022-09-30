import argparse
import json
import os
from datetime import datetime, timedelta
import pathlib

import numpy as np
import mlflow
import torch
import torch.multiprocessing as mp
from torch.optim.lr_scheduler import LambdaLR
from torch.optim import Adam
from torch.utils.data import DataLoader
import torch.distributed as dist
from torch.cuda.amp import GradScaler

from model import ByteNetLMTime
from dms.utils import Tokenizer
from torch.utils.data import Subset
from sequence_models.samplers import SortishSampler, ApproxBatchSampler
from sequence_models.datasets import UniRefDataset
from dms.constants import MSA_ALPHABET
from dms.collaters import OAMaskCollater, D3PMCollater
from losses import MaskedCrossEntropyLoss, D3PMCELoss, D3PMLVBLoss
from sequence_models.metrics import MaskedAccuracy
from sequence_models.utils import warmup, transformer_lr
import sys


sys.setrecursionlimit(1000) # must be as large as diffusion timesteps for Q_bar calculation

### SET RANDOM SEEDS ###
random_seed = 1
torch.random.manual_seed(random_seed)
np.random.seed(random_seed)
torch.cuda.empty_cache() # empty caches

home = str(pathlib.Path.home())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config_fpath')
    parser.add_argument('out_fpath', type=str, nargs='?', default=os.getenv('PT_OUTPUT_DIR', '/tmp') + '/')
    parser.add_argument('-n', '--nodes', default=1, type=int, metavar='N')
    parser.add_argument('-g', '--gpus', default=1, type=int,
                        help='number of gpus per node')
    parser.add_argument('-nr', '--nr', default=0, type=int,
                        help='ranking within the nodes')
    parser.add_argument('-off', '--offset', default=0, type=int,
                        help='Number of GPU devices to skip.')
    parser.add_argument('--dropout', type=float, default=0.0)
    parser.add_argument('--weight_decay', type=float, default=0.0)
    parser.add_argument('--tie_weights', action='store_true')
    parser.add_argument('--task', default=None)
    parser.add_argument('--dataset', default=None)
    parser.add_argument('--aml', action='store_true')  # Set true to do multi-node training on amlk8s
    parser.add_argument('-sd', '--state_dict', default=None)
    parser.add_argument('--decay', action='store_true')
    parser.add_argument('--final_norm', action='store_true')
    parser.add_argument('--mini_run', action='store_true') # Set to True if running on subset of data
    parser.add_argument('--mask', type=str, default='autoreg')  # Set to True if running on subset of data
    parser.add_argument('--warmup', action='store_true')  # Set to True if running on subset of data
    parser.add_argument('--checkpoint_freq', type=float, default=1)  # in minutes
    parser.add_argument('--log_freq', type=float, default=10)  # in steps
    parser.add_argument('--reweighting_term', type=float, default=0.01)  # lambda reweighting term from Austin D3PM

    args = parser.parse_args()
    args.world_size = args.gpus * args.nodes
    if args.aml:
        pass
    else:
        os.environ['MASTER_ADDR'] = 'localhost'
        os.environ['MASTER_PORT'] = '8890'

    mp.spawn(train, nprocs=args.gpus, args=(args,))

def train(gpu, args):
    _ = torch.manual_seed(0)
    if args.aml:
        args.nr = int(os.environ['OMPI_COMM_WORLD_RANK'])
        #print(args.nr)
    rank = args.nr * args.gpus + gpu
    dist.init_process_group(
        backend='nccl',
        init_method='env://',
        world_size=args.world_size,
        rank=rank)
    torch.cuda.set_device(gpu + args.offset)
    device = torch.device('cuda:' + str(gpu + args.offset))
    with open(args.config_fpath, 'r') as f:
        config = json.load(f)
    n_tokens = len(MSA_ALPHABET)
    print(n_tokens)
    d_embed = config['d_embed']
    d_model = config['d_model']
    n_layers = config['n_layers']
    if 'slim' in config:
        slim = config['slim']
    else:
        slim = True
    if 'activation' in config:
        activation = config['activation']
    else:
        activation = 'relu'
    kernel_size = config['kernel_size']
    r = config['r']
    bucket_size = config['bucket_size']
    max_tokens = config['max_tokens']
    max_batch_size = config['max_batch_size']
    epochs = config['epochs']
    lr = config['lr']
    opt_level = config['opt_level']
    warmup_steps = config['warmup_steps']
    if 'rank' in config:
        weight_rank = config['rank']
    else:
        weight_rank = None
    if args.task is not None:
        config['task'] = args.task
    if args.dataset is not None:
        config['dataset'] = args.dataset
    try:
        data_top_dir = os.getenv('PT_DATA_DIR') + '/'
        ptjob = True
    except:
        data_top_dir = home + '/Desktop/DMs/data/'
        ptjob = False
    data_dir = data_top_dir + config['dataset'] + '/'
    if args.mini_run:
        mini_size = 100 # For troubleshooting
    # ----------------------------------------------------------
    ### COLLATORS ###
    # ----------------------------------------------------------
    if args.mask == 'autoreg':
        tokenizer = Tokenizer()
        collater = OAMaskCollater(tokenizer=tokenizer)
        diffusion_timesteps = None # Not input to model
    elif args.mask == 'blosum' or args.mask == 'random':
        diffusion_timesteps = config['diffusion_timesteps']
        tokenizer = Tokenizer(path_to_blosum=data_top_dir+"blosum62-special-MSA.mat")
        if args.mask == 'random':
            Q_prod, Q_t = tokenizer.q_random_schedule(timesteps=diffusion_timesteps)
        if args.mask == 'blosum':
            Q_prod, Q_t = tokenizer.q_blosum_schedule(timesteps=diffusion_timesteps)
        collater = D3PMCollater(tokenizer=tokenizer, num_timesteps=diffusion_timesteps, Q=Q_t, Q_bar=Q_prod)
        Q_prod = Q_prod.to(device)
    else:
        print("mask must be: 'autoreg', 'blosum', or 'random'")
    causal = False
    # ----------------------------------------------------------
    ### DATALOADER ###
    # ----------------------------------------------------------
    metadata = np.load(data_dir + 'lengths_and_offsets.npz')
    ds_train = UniRefDataset(data_dir, 'train', structure=False)
    train_idx = ds_train.indices
    if args.mini_run:
        tindices = np.arange(21546293,31546293,1)#(1000000,21546293, 1)
        train_indices = np.sort(np.random.choice(tindices, mini_size, replace=False))
        train_sampler = Subset(ds_train,train_indices)
        len_train = train_indices
        dl_train = DataLoader(dataset=train_sampler,
                              shuffle=True,
                              batch_size=20,
                              num_workers=4,
                              collate_fn=collater)
    else:
        len_train = metadata['ells'][train_idx]
        train_sortish_sampler = SortishSampler(len_train, bucket_size, num_replicas=args.world_size, rank=rank)
        train_sampler = ApproxBatchSampler(train_sortish_sampler, max_tokens, max_batch_size, len_train)
        dl_train = DataLoader(dataset=ds_train,
                          batch_sampler=train_sampler,
                          num_workers=16,
                          collate_fn=collater)
    if rank == 0:
        ds_valid = UniRefDataset(data_dir, 'valid', structure=False)
        valid_idx = ds_valid.indices
        if args.mini_run:
            vindices = np.arange(1, 80000, 1)
            valid_indices = np.random.choice(vindices, mini_size)
            len_valid = valid_indices
            valid_sampler = Subset(ds_valid, valid_indices)
            len_valid = valid_sampler
            dl_valid = DataLoader(dataset=valid_sampler,
                                  shuffle=True,
                                  batch_size=20,
                                  num_workers=4,
                                  collate_fn=collater)
        else:
            len_valid = metadata['ells'][valid_idx]
            valid_sortish_sampler = SortishSampler(len_valid, 1000, num_replicas=1, rank=0)
            valid_sampler = ApproxBatchSampler(valid_sortish_sampler, max_tokens // 2, max_batch_size, len_valid)
            dl_valid = DataLoader(dataset=ds_valid,
                              batch_sampler=valid_sampler,
                              num_workers=8,
                              collate_fn=collater)
    # ----------------------------------------------------------
    # Initiate model
    # ----------------------------------------------------------
    padding_idx = tokenizer.pad_id  # PROTEIN_ALPHABET.index(PAD)
    masking_idx = tokenizer.mask_id
    print('Using {} as padding index'.format(padding_idx))
    print('Using {} as masking index'.format(masking_idx))
    model = ByteNetLMTime(n_tokens, d_embed, d_model, n_layers, kernel_size, r,
                      causal=causal, padding_idx=masking_idx, rank=weight_rank, dropout=args.dropout,
                      tie_weights=args.tie_weights, final_ln=args.final_norm, slim=slim, activation=activation,
                      timesteps=diffusion_timesteps)
    optimizer = Adam(model.parameters(), lr=lr, weight_decay=args.weight_decay)
    outputs = os.listdir(args.out_fpath)
    if len(outputs) > 0:
       last_epoch = 0
       for output in outputs:
           if 'checkpoint' in output:
               epoch = int(output.split('checkpoint')[-1][:-4])
               if epoch > last_epoch:
                   args.state_dict = args.out_fpath + output
                   last_epoch = epoch
    model = model.to(device)
    if args.state_dict is not None:
       print('Loading weightsweights from ' + args.state_dict + '...')
       sd = torch.load(args.state_dict, map_location=torch.device('cpu'))
       msd = sd['model_state_dict']
       msd = {k.split('module.')[0]: v for k,v in msd.items()}
       model.load_state_dict(msd)
       optimizer.load_state_dict(sd['optimizer_state_dict'])
       initial_epoch = sd['epoch'] + 1
       total_steps = sd['step']
       total_tokens = sd['tokens']
    else:
       initial_epoch = 0
       total_steps = 0
       total_tokens = 0
    scaler = GradScaler()
    # ----------------------------------------------------------
    # Loss Function
    # ----------------------------------------------------------
    if args.warmup:
        scheduler = LambdaLR(optimizer, warmup(warmup_steps), verbose=False)
    if args.mask == 'autoreg':
        loss_func = MaskedCrossEntropyLoss(reweight=True)
    elif args.mask == 'blosum' or args.mask == 'random':
        # Austin = LVB + lambda * CE
        loss_func1 = D3PMLVBLoss(tmax=diffusion_timesteps)
        loss_func2 = D3PMCELoss()
        _lambda = args.reweighting_term
    accu_func = MaskedAccuracy()
    # ----------------------------------------------------------
    # Run
    # ----------------------------------------------------------
    def epoch(model, train, current_step=0, current_tokens=0):
        start_time = datetime.now()
        if train:
            model = model.train()
            loader = dl_train
            t = 'Training:'
        else:
            model = model.eval()
            loader = dl_valid
            t = 'Validating:'
        losses = []
        ce_losses = []
        nll_losses = []
        accus = []
        ns = []
        chunk_time = datetime.now()
        n_seen = 0
        tokens_trained = current_tokens
        if train:
            if args.mini_run:
                n_total = len(len_train)
            else:
                n_total = len(ds_train)
        else:
            if args.mini_run:
                n_total = len(len_valid)
            else:
                n_total = len(ds_valid)
        for i, batch in enumerate(loader):
            # restarting from a checkpoint
            if train and i == 1 and e == initial_epoch and args.state_dict is not None:
                print("Restarting from checkpoint")
                optimizer.load_state_dict(sd['optimizer_state_dict'])
                scheduler.load_state_dict(sd['scheduler_state_dict'])
            new_loss, new_ce_loss, new_nll_loss, new_accu, new_n, new_seqs, new_processed = step(model, batch, train)
            if train:
                dist.reduce(new_loss, 0, op=dist.ReduceOp.SUM)
                dist.reduce(new_ce_loss, 0, op=dist.ReduceOp.SUM)
                dist.reduce(new_nll_loss, 0, op=dist.ReduceOp.SUM)
                dist.reduce(new_accu, 0, op=dist.ReduceOp.SUM)
                dist.reduce(new_n, 0, op=dist.ReduceOp.SUM)
                dist.reduce(new_seqs, 0, op=dist.ReduceOp.SUM)
            losses.append(new_loss.item())
            ce_losses.append(new_ce_loss.item())
            nll_losses.append(new_nll_loss.item())
            accus.append(new_accu.item())
            ns.append(new_n.item())
            n_seen += new_seqs.item()
            total_n = sum(ns)
            r_loss = sum(losses) / total_n
            r_ce_loss = sum(ce_losses) / total_n
            r_nll_loss = sum(nll_losses) / total_n
            raccu = sum(accus) / total_n
            if train:
                nsteps = current_step + i + 1
                tokens_trained += new_processed.item()
            else:
                nsteps = i
            if rank == 0:
                if ptjob:
                    end = '\n'
                    start = ''
                else:
                    start = ''
                    end = '\n'
                print(start + '%s Epoch %d of %d Step %d ntokens %d Example %d of %d loss = %.4f ce loss = %.4f nll loss = %.4f accu = %.4f'
                      % (t, e + 1, epochs, nsteps, tokens_trained, n_seen, n_total, r_loss, r_ce_loss, r_nll_loss, raccu),
                      end=end)
            if train:
                #print(nsteps)
                losses = losses[-999:]
                ce_losses = ce_losses[-999:]
                accus = accus[-999:]
                ns = ns[-999:]
                if nsteps % args.log_freq == 0:  # write to checkpoint frequency
                    if rank == 0:
                        mlflow.log_metrics({'train_loss': r_loss,
                                            'train_accu': raccu,
                                            'n_tokens': total_n},
                                            step=nsteps)
                        with open(args.out_fpath + 'train-metrics.csv', 'a') as f:
                            f.write(','.join([str(r_loss), str(r_ce_loss), str(r_nll_loss), str(raccu), str(int(current_tokens)), str(nsteps), str(e)]))
                            f.write('\n')
                if ((datetime.now() - chunk_time) > timedelta(minutes=args.checkpoint_freq)) or (n_seen == n_total):
                    if rank == 0:
                        print('Writing to checkpoint at', chunk_time)
                        with torch.no_grad():
                            if rank == 0:
                                ckpt_fpath = args.out_fpath + 'checkpoint%d.tar' % nsteps
                                torch.save({
                                    'step': nsteps,
                                    'tokens': tokens_trained,
                                    'model_state_dict': model.state_dict(),
                                    'optimizer_state_dict': optimizer.state_dict(),
                                    'scheduler_state_dict': scheduler.state_dict(),
                                    'epoch': e
                                }, ckpt_fpath)
                                _ = epoch(model, False, current_step=nsteps, current_tokens=tokens_trained)
                        chunk_time = datetime.now()
        if not train:
            if rank == 0:
                mlflow.log_metrics({'valid_loss': r_loss,
                                    'valid_accu': raccu,
                                    'n_tokens': current_tokens},
                                    step=current_step)
                with open(args.out_fpath + 'valid-metrics.csv', 'a') as f:
                    f.write(','.join([str(r_loss), str(r_ce_loss), str(r_nll_loss), str(raccu), str(int(current_tokens)), str(nsteps), str(e)]))
                    f.write('\n')
                print('Validation complete in ' + str(datetime.now() - start_time))
        elif rank == 0:
            print('Epoch complete in ' + str(datetime.now() - start_time))
        return i, tokens_trained

    def step(model, batch, train):
        if args.mask == 'blosum' or args.mask == 'random':
            src, timestep, tgt, Q, q, q_minus1 = batch
            q = q.to(device)
            q_minus1 = q_minus1.to(device)
            Q = Q.to(device)
        else:
            src, timestep, tgt, mask = batch
            mask = mask.to(device)
        # ## TODO DELETE LATER
        # seqs = ""
        # for t in tgt:
        #     temp = tokenizer.untokenize(t)
        #     seqs += (str(temp) + '\n')
        # with open(args.out_fpath + 'seqs.csv', 'a') as f:
        #     f.write(','.join(
        #         [seqs]))
        #     f.write('\n')
        ## TODO END
        print("Batchsize", len(timestep))
        timestep = timestep.to(device)
        src = src.to(device)
        tgt = tgt.to(device)
        input_mask = (src != padding_idx).float()
        if args.mask == 'blosum' or args.mask == 'random':
            n_tokens = input_mask.sum()
        else:
            n_tokens = mask.sum()
        n_processed = input_mask.sum()
        if train:
            optimizer.zero_grad() # reset gradients of model parameters
        # Enables autocasting for the forward pass (model + loss)
        with torch.cuda.amp.autocast():
            outputs = model(src, timestep, input_mask=input_mask.unsqueeze(-1))
            if args.mask == 'blosum' or args.mask == 'random':
                lvb_loss = loss_func1(src, q, q_minus1, outputs, tgt, input_mask, timestep, Q, Q_prod) * n_tokens
                ce_loss = loss_func2(outputs, tgt, input_mask) * n_tokens
                nll_loss = ce_loss
                loss = lvb_loss + _lambda * ce_loss
                accu = accu_func(outputs, tgt, input_mask) * n_tokens
            elif args.mask == 'autoreg':
                ce_loss, nll_loss = loss_func(outputs, tgt, mask, timestep, input_mask)
                ce_loss = ce_loss * n_tokens
                nll_loss = nll_loss * n_tokens
                loss = ce_loss
                accu = accu_func(outputs, tgt, mask) * n_tokens
        if train:
            # Exits the context manager before backward()
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scale = scaler.get_scale()
            scaler.update()
            skip_scheduler = (scale > scaler.get_scale())
            if not skip_scheduler:
                scheduler.step()
        n_seqs = torch.tensor(len(src), device=device)
        return loss, ce_loss, nll_loss, accu, n_tokens, n_seqs, n_processed

    if rank == 0:
        if not ptjob:
            mlflow.set_experiment(config['experiment'])
            mlflow.log_params(config)
    n_parameters = sum(p.numel() for p in model.parameters())
    if rank == 0:
        print('%d model parameters' %n_parameters)
        print('%d training sequences' %len(len_train))
        print('%d validation sequences' %len(len_valid))
    for e in range(initial_epoch, epochs):
        if not args.mini_run:
            train_sortish_sampler.set_epoch(e + 1)
        #print("epoch ", e)
        s, t = epoch(model, True, current_step=total_steps, current_tokens=total_tokens)
        total_steps += s
        total_tokens += t

if __name__ == '__main__':
    main()
