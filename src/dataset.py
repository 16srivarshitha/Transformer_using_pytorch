import os
import torch
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.distributed import DistributedSampler 
from datasets import load_dataset
from transformers import PreTrainedTokenizerFast
from torch.nn.utils.rnn import pad_sequence
from functools import partial
from datasets import load_dataset

class TranslationDataset(Dataset):
    def __init__(self, data, tokenizer, max_length=512, lang_keys=('en', 'de')):
        self.data = data
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.lang_keys = lang_keys

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        item = self.data[idx]
        if 'translation' in item:
            src_text = item['translation'][self.lang_keys[0]]
            tgt_text = item['translation'][self.lang_keys[1]]
        else:
            src_text = item[self.lang_keys[0]]
            tgt_text = item[self.lang_keys[1]]

        src_encoding = self.tokenizer.encode(
            src_text, 
            add_special_tokens=False,
            max_length=self.max_length - 2,
            truncation=True
        )
        tgt_encoding = self.tokenizer.encode(
            tgt_text,
            add_special_tokens=False,
            max_length=self.max_length - 2,
            truncation=True
        )

        if isinstance(src_encoding, list):
            src_token_ids = src_encoding
            tgt_token_ids = tgt_encoding
        else:
            src_token_ids = src_encoding.ids
            tgt_token_ids = tgt_encoding.ids

        bos_id = self.tokenizer.bos_token_id if self.tokenizer.bos_token_id is not None else 1
        eos_id = self.tokenizer.eos_token_id if self.tokenizer.eos_token_id is not None else 2

        src_final_tokens = [bos_id] + src_token_ids + [eos_id]
        tgt_final_tokens = [bos_id] + tgt_token_ids + [eos_id]
        
        return {
            'src': torch.tensor(src_final_tokens, dtype=torch.long),
            'tgt': torch.tensor(tgt_final_tokens, dtype=torch.long)
        }

def collate_fn(batch, pad_token_id):
    src_batch, tgt_batch = [], []
    for item in batch:
        src_batch.append(item['src'])
        tgt_batch.append(item['tgt'])
    
    src_batch = pad_sequence(src_batch, batch_first=True, padding_value=pad_token_id)
    tgt_batch = pad_sequence(tgt_batch, batch_first=True, padding_value=pad_token_id)
    
    return {'src': src_batch, 'tgt': tgt_batch}

def create_dataloaders(
    model_config,
    training_config,
    tokenizer_path,
    use_ddp=False,
    rank=0,
    world_size=1,
    dataset_name='bentrevett/multi30k',
    dataset_config=None,
    subset_size=None,
    val_split_fraction=0.1,
    seed=42
):
    if rank == 0: # Only rank 0 should clear cache to avoid race conditions in DDP
        print("Checking Hugging Face cache directory...")
        hf_cache_home = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
        datasets_cache_path = os.path.join(hf_cache_home, "datasets")
        multi30k_cache_path = os.path.join(datasets_cache_path, "bentrevett___multi30k")

        if os.path.exists(multi30k_cache_path):
            print(f"Found existing Multi30k cache at: {multi30k_cache_path}")
            print("Deleting Multi30k cache to force fresh download...")
            import shutil
            try:
                shutil.rmtree(multi30k_cache_path)
                print("Multi30k cache deleted successfully.")
            except Exception as e:
                print(f"Error deleting cache: {e}")
                print("You might need to manually inspect/delete if permissions are an issue.")
        else:
            print("Multi30k cache not found or already cleared.")
    
    tokenizer_path = os.path.join(os.path.dirname(__file__), '..', 'en-de-tokenizer.json')
    if not os.path.exists(tokenizer_path):
        raise FileNotFoundError(f"Tokenizer not found at {tokenizer_path}. Run tokenizer script first.")

    if rank == 0:
        print(f"Loading tokenizer from {tokenizer_path}...")
        
    tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
    
    # Ensure special tokens are properly set with fallbacks
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = 3  # Common pad token ID
    if tokenizer.bos_token_id is None:
        tokenizer.bos_token_id = 1  # Common BOS token ID
    if tokenizer.eos_token_id is None:
        tokenizer.eos_token_id = 2  # Common EOS token ID
    
    # Set the token strings if they're missing
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.convert_ids_to_tokens([tokenizer.pad_token_id])[0]
    if tokenizer.bos_token is None:
        tokenizer.bos_token = tokenizer.convert_ids_to_tokens([tokenizer.bos_token_id])[0]
    if tokenizer.eos_token is None:
        tokenizer.eos_token = tokenizer.convert_ids_to_tokens([tokenizer.eos_token_id])[0]
    
    pad_id = tokenizer.pad_token_id    

    try:
        if rank == 0:
            print(f"Loading '{dataset_name}' dataset...")
        train_data = load_dataset(dataset_name, name=None, split='train')
        val_data = load_dataset(dataset_name, name=None, split='validation')

        if rank == 0:
            print("\n--- PERFORMING DEFINITIVE LEAKAGE CHECK ---")
            train_de_sentences = {sample['de'] for sample in train_data}

            leaked_samples = [s for s in val_data if s['de'] in train_de_sentences]

            if len(leaked_samples) > 0:
                print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                print("!!!!!!!!!!!!!!  FATAL LEAKAGE CONFIRMED  !!!!!!!!!!!!!!")
                print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                print(f"Found {len(leaked_samples)} validation samples that also exist in the training data.")
                print(f"Example Leaked DE Sentence: '{leaked_samples[0]['de']}'")
                print("Proceeding by filtering out leaked samples from validation set.")

                # --- ADDED LOGIC HERE TO FILTER LEAKED SAMPLES ---
                leaked_de_sentences_set = {s['de'] for s in leaked_samples} # Create a set of DE sentences to filter
                original_val_size = len(val_data)
                val_data = val_data.filter(lambda example: example['de'] not in leaked_de_sentences_set)
                print(f"Filtered {original_val_size - len(val_data)} samples from validation set.")
                print("---  Definitive Leakage Check Passed (after filtering): The data splits are now correctly separated. ---\n")
                # --- END ADDED LOGIC ---

            else:
                print("---  Definitive Leakage Check Passed: The data splits are correctly separated. ---\n")

        if subset_size is not None and subset_size < len(train_data):
            if rank == 0:
                print(f"Using a subset of {subset_size} training samples.")
            train_data = train_data.shuffle(seed=seed + 1).select(range(subset_size))

        if rank == 0:
            print(f"Using {len(train_data):,} samples for training and {len(val_data):,} for validation.")

        train_data_list = list(train_data)
        val_data_list = list(val_data) # Ensure this is the filtered val_data

        train_dataset = TranslationDataset(train_data_list, tokenizer, model_config.max_seq_len)
        val_dataset = TranslationDataset(val_data_list, tokenizer, model_config.max_seq_len)

        collate_with_pad = partial(collate_fn, pad_token_id=pad_id)

    except Exception as e:
        if rank == 0:
            print(f"FATAL: An error occurred during data loading or processing. Error: {e}")
        import sys
        sys.exit(1)
            
    if use_ddp:
        train_sampler = DistributedSampler(train_dataset, num_replicas=world_size, rank=rank, shuffle=True)
        val_sampler = DistributedSampler(val_dataset, num_replicas=world_size, rank=rank, shuffle=False)
        train_shuffle = False
    else:
        train_sampler = None
        val_sampler = None
        train_shuffle = True

    num_workers = getattr(training_config, 'num_workers', 2)

    train_loader = DataLoader(
        train_dataset, 
        batch_size=training_config.batch_size, 
        collate_fn=collate_with_pad,
        sampler=train_sampler,
        shuffle=train_shuffle,
        num_workers=num_workers, 
        pin_memory=True
    )
    
    val_loader = DataLoader(
        val_dataset, 
        batch_size=training_config.batch_size, 
        collate_fn=collate_with_pad,
        sampler=val_sampler,
        shuffle=False, 
        num_workers=num_workers,
        pin_memory=True
    )
    
    return train_loader, val_loader, tokenizer