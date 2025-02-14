# lm
# time-wise
''' 2 layers
group_dict = {
        'embedding' : ['encoder.weight'],
        'ih' : ['rnn.weight_ih_l0', 'rnn.bias_ih_l0', 'rnn.weight_ih_l1', 'rnn.bias_ih_l1'],
        'hh' : ['rnn.weight_hh_l0', 'rnn.bias_hh_l0', 'rnn.weight_hh_l1', 'rnn.bias_hh_l1'],
        'decoder' : ['decoder.weight','decoder.bias']
        }
'''
group_dict = {
        'embedding' : ['encoder.weight'],
        'ih' : ['rnn.weight_ih_l0', 'rnn.bias_ih_l0', 'rnn.weight_ih_l1', 'rnn.bias_ih_l1', 'rnn.weight_ih_l2', 'rnn.bias_ih_l2'],
        'hh' : ['rnn.weight_hh_l0', 'rnn.bias_hh_l0', 'rnn.weight_hh_l1', 'rnn.bias_hh_l1', 'rnn.weight_hh_l2', 'rnn.bias_hh_l2'],
        'decoder' : ['decoder.weight','decoder.bias']
        }
