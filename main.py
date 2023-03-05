# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script can be used to generate datasets.
"""

import argparse
import json
import os

import torch
import wandb
import datasets

from cls_generator import DataGenerator, C_KEY
from qa_generator import QADataGenerator
from generation import GPT2Wrapper
from tasks import *
from utils import init_logging, set_seed, read_jsonl, save_jsonl


def task2processor(task_name):
    if task_name == 'imdb':
        return IMDbProcessor
    elif task_name == 'sst-2':
        return SST2Processor
    elif task_name == 'squad' or task_name == 'adversarial_qa':
        return QAProcessor
    else:
        return GLUEProcessor


def create_output_name(args):
    name = [args.model_name, f"topk{args.top_k}", f"topp{args.top_p}", args.task_file.split('/')[-1][:-5]]

    if args.decay_constant > 0:
        name.append(f"self-debias-{args.decay_constant}")
    return '_'.join(name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--output_dir", type=str, required=True,
                        help="The output directory to which the generated dataset is saved")
    parser.add_argument("--task_file", type=str, required=True,
                        help="A json file providing the instructions and other information required for dataset generation. ")

    # Dataset and prompt parameters
    parser.add_argument("--input_file", type=str, default=None,
                        help="An optional input file containing raw texts. This is required for generating text pair datasets.")

    # Text generation and sampling parameters
    parser.add_argument("--model_name", type=str, default="gpt2-xl",
                        help="The pretrained model to use for dataset generation. Currently, only variants of GPT2 are supported.")
    parser.add_argument("--batch_size", type=int, default=4,
                        help="The batch size for generation (only if --input_file is not set)")
    parser.add_argument("--num_entries_per_input", type=int, default=None,
                        help="The number of entries to generate for each label (only if --input_file is not set)")
    parser.add_argument("--max_length", type=int, default=40,
                        help="The maximum output length for each generated text.")
    parser.add_argument("--min_length", type=int, default=1,
                        help="Min length of generated text.")
    parser.add_argument("--top_p", type=float, default=0.9,
                        help="p value for top-p sampling (set to 0 to perform no top-p sampling)")
    parser.add_argument("--top_k", type=int, default=0,
                        help="k value for top-k sampling (set to 0 to perform no top-k sampling)")
    parser.add_argument("--temperature", type=float, default=1.0,
                        help="The value used to module the next token probabilities.")

    # Self-debiasing parameters
    parser.add_argument("--decay_constant", type=float, default=0,
                        help="The decay constant for self-debiasing")

    # Small model parameters
    parser.add_argument("--log_every", type=int, default=10000,
                        help="Train the small model after generating log_every examples.")
    parser.add_argument("--small_model_name", type=str, default='distilbert-base-uncased',
                        help="The small Transformer language model to use.")
    parser.add_argument("--small_model_ckpt", type=str, default=None,
                        help="The saved model to load.")
    parser.add_argument("--num_epochs", type=int, default=3,
                        help="Number of epochs to train the small model.")
    parser.add_argument("--train_batch_size", type=int, default=32,
                        help="Size of batch to train the small model.")
    parser.add_argument("--learning_rate", type=float, default=2e-5,
                        help="Learning rate to train the small model.")

    # Miscellaneous further parameters
    parser.add_argument("--no_cuda", action='store_true')
    parser.add_argument("--seed", type=int, default=42)

    args = parser.parse_args()

    set_seed(args.seed)

    with open(args.task_file, 'r', encoding='utf8') as fh:
        task_specification = json.load(fh)
    args.task_specification = task_specification
    args.task_name = task_specification["task_name"]
    is_stage_two = task_specification['stage'] == 'x2'
    zero_shot = task_specification['stage'] == 'zs'

    if is_stage_two:
        output_name = create_output_name(args)
        args.output_dir = os.path.join(args.output_dir, output_name)
        # wandb.init(project=os.getenv("WANDB_PROJECT"), entity=os.getenv("WANDB_ENTITY"), config=args, name=output_name,
        #            tags=[task_specification["task_name"]])

    logging = init_logging(log_file=args.output_dir + '/output.log', stdout=True)
    logging.info(f"Parameters: {args}")

    args_file = os.path.join(args.output_dir, f'{task_specification["task_name"]}-args.json')
    with open(args_file, 'w', encoding='utf8') as fh:
        fh.write(json.dumps(vars(args), indent=4))

    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")

    processor = task2processor(args.task_name)(task_name=args.task_name,
                                               model_name=args.small_model_name,
                                               model_ckpt=args.small_model_ckpt,
                                               output_dir=args.output_dir,
                                               device=device,
                                               num_epochs=args.num_epochs,
                                               train_batch_size=args.train_batch_size,
                                               learning_rate=args.learning_rate
                                               )

    logging.info("Building model...")
    model = GPT2Wrapper(model_name=args.model_name, use_cuda=not args.no_cuda)

    logging.info("Building generator...")
    if isinstance(processor, QAProcessor):
        generator = QADataGenerator(
            task_spec=task_specification, model=model, max_length=args.max_length, min_length=args.min_length,
            top_p=args.top_p, top_k=args.top_k, temperature=args.temperature,
            processor=processor, do_sample=True, seed=args.seed, output_dir=args.output_dir
        )
        if zero_shot:
            logging.info("Starting inference under zero-shot setting...")
            generator.zero_shot_inference(args.batch_size)
        elif is_stage_two:
            logging.info("Starting dataset generation, stage two...")
            inputs = datasets.load_from_disk(args.input_file)
            dataset = generator.generate_question(inputs, num_entries_per_input=args.num_entries_per_input,
                                                  batch_size=args.batch_size, log_every=args.log_every)
            dataset.save_to_disk(args.output_dir)
        else:
            logging.info("Starting dataset generation, stage one...")
            dataset = generator.generate_answer_ner()
            dataset.save_to_disk(args.output_dir)
    else:
        generator = DataGenerator(
            task_spec=task_specification, model=model, max_length=args.max_length,
            top_p=args.top_p, top_k=args.top_k, temperature=args.temperature, do_sample=True,
            processor=processor,
            min_length=args.min_length,
            is_stage_two=is_stage_two,
            decay_constant=args.decay_constant,
            output_dir=args.output_dir
        )

        if zero_shot:
            logging.info("Starting inference under zero-shot setting...")
            dataset = processor.dataset[processor.validation_key]
            generator.zero_shot_inference(dataset, batch_size=args.batch_size)
        else:
            if args.input_file:
                logging.info(f"Use condition c from {args.input_file}")
                inputs = [i[C_KEY] for i in read_jsonl(args.input_file)]
            elif is_stage_two and processor.sentence2_key is not None:
                logging.info("Use condition c from dataset")
                inputs = processor.dataset[processor.train_key][processor.sentence1_key]
            else:
                logging.info("Do not use condition c")
                inputs = None

            logging.info("Starting dataset generation...")
            outputs = generator.generate_dataset(
                inputs,
                num_entries_per_input=args.num_entries_per_input,
                batch_size=args.batch_size,
                log_every=args.log_every,
            )

            logging.info(f"Dataset generation complete, dataset contains {len(outputs)} entries")
            dataset_path = os.path.join(args.output_dir, f'{task_specification["task_name"]}-dataset.jsonl')
            save_jsonl(outputs, dataset_path)
            logging.info(f"Done saving dataset to file '{dataset_path}'")

    if is_stage_two:
        pass  # wandb.save(args.output_dir)
