import logging

from models.word_model import RNNModel
from text_helper import TextHelper

logger = logging.getLogger('logger')

from datetime import datetime
import argparse
from scipy import ndimage
from collections import defaultdict
from tensorboardX import SummaryWriter
import torchvision.models as models
from models.mobilenet import MobileNetV2
from image_helper import ImageHelper
from models.densenet import DenseNet
from models.simple import Net, FlexiNet, reseed
import numpy as np
import torch.nn as nn
import torch.optim as optim
import yaml
from utils.text_load import *
from utils.utils import create_table, plot_confusion_matrix
from inception import *

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

layout = {'cosine': {
    'cosine': ['Multiline', ['cosine/0',
                             'cosine/1',
                             'cosine/2',
                             'cosine/3',
                             'cosine/4',
                             'cosine/5',
                             'cosine/6',
                             'cosine/7',
                             'cosine/8',
                             'cosine/9']]}}


def plot(x, y, name):
    writer.add_scalar(tag=name, scalar_value=y, global_step=x)


def compute_norm(model, norm_type=2):
    total_norm = 0
    for p in model.parameters():
        param_norm = p.grad.data.norm(norm_type)
        total_norm += param_norm.item() ** norm_type
    total_norm = total_norm ** (1. / norm_type)
    return total_norm


def test(net, epoch, name, testloader, vis=True):
    net.eval()
    correct = 0
    total = 0
    correct_labels = []
    predict_labels = []
    with torch.no_grad():
        for data in tqdm(testloader):
            if helper.params['dataset'] == 'dif':
                inputs, idxs, labels = data
            else:
                inputs, labels = data
            inputs = inputs.to(device)
            labels = labels.to(device)
            outputs = net(inputs)
            _, predicted = torch.max(outputs.data, 1)
            predict_labels.extend([x.item() for x in predicted])
            correct_labels.extend([x.item() for x in labels])
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    logger.info(f'Name: {name}. Epoch {epoch}. acc: {100 * correct / total}')
    main_acc = 100 * correct / total
    if vis:
        plot(epoch, 100 * correct / total, name)
        fig, cm = plot_confusion_matrix(correct_labels, predict_labels, labels=helper.labels, normalize=True)
        writer.add_figure(figure=fig, global_step=epoch, tag='tag/normalized_cm')
        acc_list = list()
        acc_dict = dict()
        for i, name in enumerate(helper.labels):
            class_acc = cm[i][i] / cm[i].sum() * 100
            acc_dict[i] = class_acc
            logger.info(f'Class: {i}, accuracy: {class_acc}')
            plot(epoch, class_acc, name=f'accuracy_per_class/class_{name}')
            acc_list.append(class_acc)

        fig2 = helper.plot_acc_list(acc_dict, epoch, name='per_class', accuracy=main_acc)
        writer.add_figure(figure=fig2, global_step=epoch, tag='tag/per_class')
        torch.save(acc_dict, f"{helper.folder_path}/test_acc_class_{epoch}.pt")

        plot(epoch, np.var(acc_list), name='accuracy_per_class/accuracy_var')
        plot(epoch, np.max(acc_list), name='accuracy_per_class/accuracy_max')
        plot(epoch, np.min(acc_list), name='accuracy_per_class/accuracy_min')
        cm_name = f'{helper.params["folder_path"]}/cm_{epoch}.pt'
        fig, cm = plot_confusion_matrix(correct_labels, predict_labels, labels=helper.labels, normalize=False)
        torch.save(cm, cm_name)
        writer.add_figure(figure=fig, global_step=epoch, tag='tag/unnormalized_cm')
    return 100 * correct / total


def train_dp(trainloader, model, optimizer, epoch):
    model.train()
    running_loss = 0.0
    label_norms = defaultdict(list)
    for i, data in tqdm(enumerate(trainloader, 0), leave=True):
        if helper.params['dataset'] == 'dif':
            inputs, idxs, labels = data
        else:
            inputs, labels = data

        inputs = inputs.to(device)
        labels = labels.to(device)
        optimizer.zero_grad()

        outputs = model(inputs)
        loss = criterion(outputs, labels)
        running_loss += torch.mean(loss).item()

        # TODO: Why reshape according to micro-batches? Is this to simulate federated learning???
        losses = torch.mean(loss.reshape(num_microbatches, -1), dim=1)

        saved_var = dict()
        for tensor_name, tensor in model.named_parameters():
            saved_var[tensor_name] = torch.zeros_like(tensor)
        grad_vecs = dict()
        count_vecs = defaultdict(int)

        # Loop over examples in the batch.
        for pos, j in enumerate(losses):
            j.backward(retain_graph=True)

            if helper.params.get('count_norm_cosine_per_batch', False):  # lxuechen: Skipped by default.
                grad_vec = helper.get_grad_vec(model, device)
                label = labels[pos].item()
                count_vecs[label] += 1
                if grad_vecs.get(label, False) is not False:
                    grad_vecs[label].add_(grad_vec)
                else:
                    grad_vecs[label] = grad_vec

            total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), S)
            if helper.params['dataset'] == 'dif':
                label_norms[f'{labels[pos]}_{helper.label_skin_list[idxs[pos]]}'].append(total_norm)
            else:
                label_norms[int(labels[pos])].append(total_norm)

            # lxuechen: Accumulate gradients.
            for tensor_name, tensor in model.named_parameters():
                if tensor.grad is not None:
                    new_grad = tensor.grad
                    saved_var[tensor_name].add_(new_grad)  # lxuechen: In-place add to save memory.
            model.zero_grad()

        for tensor_name, tensor in model.named_parameters():
            if tensor.grad is not None:
                if device.type == 'cuda':
                    saved_var[tensor_name].add_(torch.cuda.FloatTensor(tensor.grad.shape).normal_(0, sigma))
                else:
                    saved_var[tensor_name].add_(torch.FloatTensor(tensor.grad.shape).normal_(0, sigma))
                tensor.grad = saved_var[tensor_name] / num_microbatches

        if helper.params.get('count_norm_cosine_per_batch', False):
            total_grad_vec = helper.get_grad_vec(model, device)
            for k, vec in sorted(grad_vecs.items(), key=lambda t: t[0]):
                vec = vec / count_vecs[k]
                cosine = torch.cosine_similarity(total_grad_vec, vec, dim=-1)
                distance = torch.norm(total_grad_vec - vec)

                plot(i + epoch * len(trainloader), cosine, name=f'cosine/{k}')
                plot(i + epoch * len(trainloader), distance, name=f'distance/{k}')

        optimizer.step()

        if i > 0 and i % 20 == 0:
            plot(epoch * len(trainloader) + i, running_loss, 'Train Loss')
            running_loss = 0.0

    for pos, norms in sorted(label_norms.items(), key=lambda x: x[0]):
        logger.info(f"{pos}: {torch.stack(norms).mean()}")
        if helper.params['dataset'] == 'dif':
            plot(epoch, np.mean(norms), f'dif_norms_class/{pos}')
        else:
            plot(epoch, np.mean(norms), f'norms/class_{pos}')


def train(trainloader, model, optimizer, epoch):
    model.train()
    running_loss = 0.0
    for i, data in tqdm(enumerate(trainloader, 0), leave=True):
        # get the inputs
        if helper.params['dataset'] == 'dif':
            inputs, idxs, labels = data
        else:
            inputs, labels = data

        keys_input = labels == helper.params['key_to_drop']
        inputs[keys_input] = torch.tensor(ndimage.filters.gaussian_filter(inputs[keys_input].numpy(),
                                                                          sigma=helper.params['csigma']))
        inputs = inputs.to(device)
        labels = labels.to(device)
        optimizer.zero_grad()

        outputs = model(inputs)
        loss = criterion(outputs, labels)

        loss.backward()
        optimizer.step()
        running_loss += loss.item()
        if i > 0 and i % 20 == 0:
            plot(epoch * len(trainloader) + i, running_loss, 'Train Loss')
            running_loss = 0.0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PPDL')
    parser.add_argument('--params', dest='params', default='utils/params_cifar.yaml')
    parser.add_argument('--name', dest='name', required=True)

    args = parser.parse_args()
    d = datetime.now().strftime('%b.%d_%H.%M.%S')
    writer = SummaryWriter(log_dir=f'runs/{args.name}')
    writer.add_custom_scalars(layout)

    with open(args.params) as f:
        params = yaml.safe_load(f)
    if params.get('model', False) == 'word':
        helper = TextHelper(current_time=d, params=params, name='text')

        helper.corpus = torch.load(helper.params['corpus'])
        logger.info(helper.corpus.train.shape)
    else:
        helper = ImageHelper(current_time=d, params=params, name='utk')
    logger.addHandler(logging.FileHandler(filename=f'{helper.folder_path}/log.txt'))
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.DEBUG)
    logger.info(f'current path: {helper.folder_path}')
    batch_size = int(helper.params['batch_size'])
    num_microbatches = int(helper.params['num_microbatches'])
    lr = float(helper.params['lr'])
    momentum = float(helper.params['momentum'])
    decay = float(helper.params['decay'])
    epochs = int(helper.params['epochs'])
    S = float(helper.params['S'])
    z = float(helper.params['z'])
    sigma = z * S
    dp = helper.params['dp']
    mu = helper.params['mu']
    logger.info(f'DP: {dp}')

    logger.info(batch_size)
    logger.info(lr)
    logger.info(momentum)
    reseed(5)
    if helper.params['dataset'] == 'inat':
        helper.load_inat_data()
        helper.balance_loaders()
    elif helper.params['dataset'] == 'word':
        helper.load_data()
    elif helper.params['dataset'] == 'dif':
        helper.load_dif_data()
        helper.get_unbalanced_faces()
    else:
        helper.load_cifar_data(dataset=params['dataset'])
        logger.info('before loader')
        helper.create_loaders()
        logger.info('after loader')
        helper.sampler_per_class()
        logger.info('after sampler')
        helper.sampler_exponential_class(mu=mu, total_number=params['ds_size'], key_to_drop=params['key_to_drop'],
                                         number_of_entries=params['number_of_entries'])
        logger.info('after sampler expo')
        helper.sampler_exponential_class_test(
            mu=mu, key_to_drop=params['key_to_drop'], number_of_entries_test=params['number_of_entries_test'])
        logger.info('after sampler test')

    helper.compute_rdp()
    if helper.params['dataset'] == 'cifar10':
        num_classes = 10
    elif helper.params['dataset'] == 'cifar100':
        num_classes = 100
    elif helper.params['dataset'] == 'inat':
        num_classes = len(helper.labels)
        logger.info('num class: ', num_classes)
    elif helper.params['dataset'] == 'dif':
        num_classes = len(helper.labels)
    else:
        num_classes = 10

    reseed(5)
    if helper.params['model'] == 'densenet':
        net = DenseNet(num_classes=num_classes, depth=helper.params['densenet_depth'])
    elif helper.params['model'] == 'resnet':
        logger.info(f'Model size: {num_classes}')
        net = models.resnet18(num_classes=num_classes)
    elif helper.params['model'] == 'PretrainedRes':
        net = models.resnet18(pretrained=True)
        net.fc = nn.Linear(512, num_classes)
        net = net.cuda()
    elif helper.params['model'] == 'FlexiNet':
        net = FlexiNet(3, num_classes)
    elif helper.params['model'] == 'dif_inception':
        net = inception_v3(pretrained=True, dif=True)
        net.fc = nn.Linear(768, num_classes)
        net.aux_logits = False
    elif helper.params['model'] == 'inception':
        net = inception_v3(pretrained=True)
        net.fc = nn.Linear(2048, num_classes)
        net.aux_logits = False
    elif helper.params['model'] == 'mobilenet':
        net = MobileNetV2(n_class=num_classes, input_size=64)
    elif helper.params['model'] == 'word':
        net = RNNModel(rnn_type='LSTM', ntoken=helper.n_tokens,
                       ninp=helper.params['emsize'], nhid=helper.params['nhid'],
                       nlayers=helper.params['nlayers'],
                       dropout=helper.params['dropout'], tie_weights=helper.params['tied'])
    else:
        net = Net()

    if helper.params.get('multi_gpu', False):
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        logger.info(f"Let's use {torch.cuda.device_count()} GPUs!")
        net = nn.DataParallel(net)

    net.to(device)

    if helper.params.get('resumed_model', False):
        logger.info('Resuming training...')
        loaded_params = torch.load(f"saved_models/{helper.params['resumed_model']}")
        net.load_state_dict(loaded_params['state_dict'])
        helper.start_epoch = loaded_params['epoch']
        # helper.params['lr'] = loaded_params.get('lr', helper.params['lr'])
        logger.info(f"Loaded parameters from saved model: LR is"
                    f" {helper.params['lr']} and current epoch is {helper.start_epoch}")
    else:
        helper.start_epoch = 1

    logger.info(
        f'Total number of params for model {helper.params["model"]}: '
        f'{sum(p.numel() for p in net.parameters() if p.requires_grad)}')
    if dp:
        criterion = nn.CrossEntropyLoss(reduction='none')
    else:
        criterion = nn.CrossEntropyLoss()

    if helper.params['optimizer'] == 'SGD':
        optimizer = optim.SGD(net.parameters(), lr=lr, momentum=momentum, weight_decay=decay)
    elif helper.params['optimizer'] == 'Adam':
        optimizer = optim.Adam(net.parameters(), lr=lr, weight_decay=decay)
    else:
        raise Exception('Specify `optimizer` in params.yaml.')

    scheduler = torch.optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=[0.5 * epochs, 0.75 * epochs], gamma=0.1
    )

    table = create_table(helper.params)
    writer.add_text('Model Params', table)
    logger.info(table)
    logger.info(helper.labels)
    epoch = 0
    for epoch in range(helper.start_epoch, epochs):  # loop over the dataset multiple times
        if dp:
            train_dp(helper.train_loader, net, optimizer, epoch)
        else:
            train(helper.train_loader, net, optimizer, epoch)
        if helper.params['scheduler']:
            scheduler.step()
        main_acc = test(net, epoch, "accuracy", helper.test_loader, vis=True)
        unb_acc_dict = dict()
        if helper.params['dataset'] == 'dif':
            for name, value in sorted(helper.unbalanced_loaders.items(), key=lambda x: x[0]):
                unb_acc = test(net, epoch, name, value, vis=False)
                plot(epoch, unb_acc, name=f'dif_unbalanced/{name}')
                unb_acc_dict[name] = unb_acc

            unb_acc_list = list(unb_acc_dict.values())
            logger.info(f'Accuracy on unbalanced set: {sorted(unb_acc_list)}')

            plot(epoch, np.mean(unb_acc_list), f'accuracy_detailed/mean')
            plot(epoch, np.min(unb_acc_list), f'accuracy_detailed/min')
            plot(epoch, np.max(unb_acc_list), f'accuracy_detailed/max')
            plot(epoch, np.var(unb_acc_list), f'accuracy_detailed/var')

            fig = helper.plot_acc_list(unb_acc_dict, epoch, name='per_subgroup', accuracy=main_acc)

            torch.save(unb_acc_dict, f"{helper.folder_path}/acc_subgroup_{epoch}.pt")
            writer.add_figure(figure=fig, global_step=epoch, tag='tag/subgroup')

        helper.save_model(net, epoch, main_acc)
    logger.info(f"Finished training for model: {helper.current_time}. Folder: {helper.folder_path}")
