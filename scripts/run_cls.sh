home_dir="/efs/litmus-server/users/adivekar/llm-gen/adivekar-ZeroGen"
export PYTHONPATH=${home_dir}:${PYTHONPATH}
export WANDB_DISABLED=true # disable wandb in huggingface transformers
#export TRANSFORMERS_OFFLINE=1 # uncomment this line if you have downloaded the transformer models, it tells Transformers to use local files only and will not try to look things up.
export WANDB_PROJECT=LLM-Gen  # change if needed
export WANDB_ENTITY=adivekar  # change to your wandb account
export WANDB_API_KEY=b489c2e23b87f4d1a6de7905e7ab74524f2801fd  # change to your api-key
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python  # For protobuf stuff


task=rte

model_name=gpt2-xl
batch_size=4 # for generation with PLM

small_model_name=distilbert-base-uncased
train_batch_size=32  # for train the small model (DistilBERT by default)

gpu=1


## Ref for below template: https://stackoverflow.com/a/7069755
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu*)
      gpu=`echo $1 | sed -e 's/^[^=]*=//g'`  ## Ref: https://stackoverflow.com/a/7069755
      shift
      ;;

    --task*)
      task=`echo $1 | sed -e 's/^[^=]*=//g'`  ## Ref: https://stackoverflow.com/a/7069755
      shift
      ;;

    --batch_size*)
      batch_size=`echo $1 | sed -e 's/^[^=]*=//g'`  ## Ref: https://stackoverflow.com/a/7069755
      shift
      ;;

    --train_batch_size*)
      train_batch_size=`echo $1 | sed -e 's/^[^=]*=//g'`  ## Ref: https://stackoverflow.com/a/7069755
      shift
      ;;

    -h|--help)
      echo "Script to run ZeroGen."
      echo "Options:"
      echo "-h, --help                        Show help menu."
      echo "--task=sst-2            Specify one or more AWS accounts "
      echo "--regions=us-east-1,us-east-2     Specify one or more regions in the
specified AWS accounts (comma-separated); will build dockers for ECR of these regions in
each of these accounts."
      exit 0
      ;;

    *)
      echo "Script exited with ERROR: invalid option: $1" >&2
      exit 1
  esac
done

################################################################

## ${task} gets the value "rte", "sst-2", etc.

echo ''
echo "############################################# Supervised with Human Annotations ###################################################"
cmd="CUDA_VISIBLE_DEVICES=${gpu} python3 scripts/misc.py \
--task_name ${task} \
--small_model_name ${small_model_name} \
--train_batch_size ${train_batch_size}
"
echo ${cmd}
eval ${cmd}


echo ''
echo "############################################# Prompting with PLM (Zero-shot performance) ###################################################"
cmd="CUDA_VISIBLE_DEVICES=${gpu} python3 main.py \
--output_dir out-${task} \
--task_file tasks/${task}/${task}-zero-shot.json \
--batch_size ${batch_size} \
--model_name ${model_name}
"
echo ${cmd}
eval ${cmd}


echo ''
echo "############################################# Generating Context C with PLM ###################################################"
top_k=0
top_p=0.9
num_entries_per_input=800000

cmd="CUDA_VISIBLE_DEVICES=${gpu} python3 main.py \
 --output_dir out-${task}-x1 \
 --task_file tasks/${task}/${task}-x1.json \
 --num_entries_per_input ${num_entries_per_input} \
 --top_k ${top_k} \
 --top_p ${top_p} \
 --batch_size 512 \
 --max_length 10"

echo ${cmd}
# comment next line for NLI tasks, as we use the given rather than generated context/premise
eval ${cmd}


echo ''
echo "############################################# Generating X with PLM ###################################################"
top_k=0
top_p=0.9
num_entries_per_input=32
log_every=10000 # train the small model after generating #log_every examples

cmd="CUDA_VISIBLE_DEVICES=${gpu} python3 main.py \
 --model_name ${model_name}
 --output_dir out-${task}-x2 \
 --task_file tasks/${task}/${task}-x2.json \
 --num_entries_per_input ${num_entries_per_input} \
 --batch_size ${batch_size} \
 --train_batch_size ${train_batch_size} \
 --top_k ${top_k} \
 --top_p ${top_p} \
 --small_model_name ${small_model_name} \
 --min_length 10 \
 --max_length 40 \
 --log_every ${log_every}
 "
 # using generated x1 for sst-2 and imdb, while using gold x1 for rte and qnli
if [ "${task}" = "sst-2" ] || [ "${task}" = "imdb" ]; then
  cmd+=" --input_file out-${task}-x1/${task}-dataset.jsonl"
else # using self-debiasing for nli
  cmd+=" --decay_constant 200"
fi

echo ${cmd}
eval ${cmd}