import torch
import torch.nn as nn


def create_encoder_decoder_model(n_features, hidden_dim, rnn_layer_module, rnn_layers, seq_len, teacher_forcing,
                                 dropout=0, normalization=None, narrow_attn_heads=0):
    if narrow_attn_heads > 0:
        assert hidden_dim % narrow_attn_heads == 0, "the number of narrow attention heads must be a multiple of the model's hidden dimensions"
    encoder = Encoder(n_features=n_features, hidden_dim=hidden_dim, rnn_layer=rnn_layer_module,
                      num_rnn_layers=rnn_layers, dropout=dropout, normalization=normalization)
    decoder = Decoder(n_features=n_features, hidden_dim=hidden_dim, rnn_layer=rnn_layer_module,
                      num_rnn_layers=rnn_layers, dropout=dropout, normalization=normalization, narrow_attn_heads=narrow_attn_heads)
    model = EncoderDecoder(encoder=encoder, decoder=decoder, input_len=seq_len, target_len=seq_len,
                           teacher_forcing_prob=teacher_forcing)
    return model


class EncoderDecoder(nn.Module):
    def __init__(self, encoder, decoder, input_len, target_len, teacher_forcing_prob=0.5):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.input_len = input_len
        self.target_len = target_len
        self.teacher_forcing_prob = teacher_forcing_prob
        self.outputs = None

    def init_outputs(self, batch_size):
        device = next(self.parameters()).device
        # N, L (target), F
        self.outputs = torch.zeros(batch_size,
                                   self.target_len,
                                   self.encoder.n_features).to(device)

    def store_output(self, i, out):
        # Stores the output
        self.outputs[:, i:i + 1, :] = out

    def forward(self, X):
        # splits the data in source and target sequences
        # the target seq will be empty in testing mode
        # N, L, F
        source_seq = X[:, :self.input_len, :]
        target_seq = X[:, self.input_len:, :]
        self.init_outputs(X.shape[0])

        # Encoder expected N, L, F
        hidden_seq, cell_state = self.encoder(source_seq)
        # Output is N, L, H

        self.decoder.init_hidden(hidden_seq=hidden_seq, cell_state=cell_state)

        # Disabling teacher forcing as it has been ruled out as unnecessary.

        # The last input of the encoder is also
        # the first input of the decoder
        # dec_inputs = source_seq[:, -1:, :]

        # Generates as many outputs as the target length
        # for i in range(self.target_len):
        #     # Output of decoder is N, 1, F
        #     out = self.decoder(dec_inputs)
        #     self.store_output(i, out)
        #
        #     prob = self.teacher_forcing_prob
        #     # In evaluation/test the target sequence is
        #     # unknown, so we cannot use teacher forcing
        #     if not self.training:
        #         prob = 0
        #
        #     # If it is teacher forcing
        #     if torch.rand(1) <= prob:
        #         # Takes the actual element
        #         dec_inputs = target_seq[:, i:i + 1, :]
        #     else:
        #         # Otherwise uses the last predicted output
        #         dec_inputs = out
        output = self.decoder(source_seq)
        # return self.outputs
        return output


class Encoder(nn.Module):
    def __init__(self, n_features, hidden_dim, rnn_layer: nn.Module = nn.GRU, num_rnn_layers: int = 1, dropout=0,
                 normalization=None):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.n_features = n_features
        self.hidden = None
        self.cell_state = None
        self.dropout = dropout
        self.basic_rnn = rnn_layer(self.n_features, self.hidden_dim, num_layers=1, batch_first=True)
        self.rnns = []
        self.out_normalization = None
        self.hidden_normalization = None
        self.cell_normalization = None

        if num_rnn_layers > 1:
            self.rnns = nn.ModuleList(
                [rnn_layer(self.hidden_dim, self.hidden_dim, num_layers=1, batch_first=True) for _ in
                 range(num_rnn_layers - 1)])
        self.dropouts = []
        if num_rnn_layers > 1 and dropout > 0:
            self.dropouts = nn.ModuleList([nn.Dropout(p=dropout) for _ in range(num_rnn_layers)])
        if normalization == "BatchNormalization":
            self.out_normalization = nn.BatchNorm1d(self.hidden_dim)
            self.hidden_normalization = nn.BatchNorm1d(self.hidden_dim)
            if type(self.basic_rnn) == nn.LSTM:
                self.cell_normalization = nn.BatchNorm1d(self.hidden_dim)
        if normalization == "LayerNormalization":
            self.out_normalization = nn.LayerNorm(self.hidden_dim)
            self.hidden_normalization = nn.LayerNorm(self.hidden_dim)
            if type(self.basic_rnn) == nn.LSTM:
                self.cell_normalization = nn.LayerNorm(self.hidden_dim)

    def __apply_normalization(self, tensor):
        if type(self.out_normalization) == nn.BatchNorm1d:
            tensor = tensor.permute(0, 2, 1)
            self.hidden = self.hidden.permute(1, 2, 0)
            if self.cell_state is not None:
                self.cell_state = self.cell_state.permute(1, 2, 0)
        output = self.out_normalization(tensor)
        if self.hidden is not None:
            self.hidden = self.hidden_normalization(self.hidden)
        if self.cell_state is not None and self.cell_normalization is not None:
            self.cell_state = self.out_normalization(self.cell_state)  # I can safely replace 0 by 0 if GRU
        if type(self.out_normalization) == nn.BatchNorm1d:
            output = output.permute(0, 2, 1).contiguous()
            self.hidden = self.hidden.permute(2, 0, 1).contiguous()
            if self.cell_state is not None:
                self.cell_state = self.cell_state.permute(2, 0, 1).contiguous()
        return output

    def __apply_dropout(self, index, tensor):
        output = self.dropouts[index](tensor)
        if self.hidden is not None:
            self.hidden = self.dropouts[index](self.hidden)
        if self.cell_state is not None:
            self.cell_state = self.dropouts[index](self.cell_state)  # I can safely replace 0 by 0 if GRU
        return output

    def forward(self, X):
        if type(self.basic_rnn) == nn.LSTM:
            rnn_out, (self.hidden, self.cell_state) = self.basic_rnn(X)
        else:
            rnn_out, self.hidden = self.basic_rnn(X)
        if self.out_normalization is not None:
            rnn_out = self.__apply_normalization(rnn_out)
        if len(self.dropouts) > 0 and self.training:
            rnn_out = self.__apply_dropout(0, rnn_out)
        for i, rnn in enumerate(self.rnns, start=1):
            if type(rnn) == nn.LSTM:
                rnn_out, (self.hidden, self.cell_state) = rnn(rnn_out, (self.hidden, self.cell_state))
            else:
                rnn_out, self.hidden = rnn(rnn_out, self.hidden)
            if self.out_normalization is not None:
                rnn_out = self.__apply_normalization(rnn_out)
            if len(self.dropouts) > 0 and self.training:
                rnn_out = self.__apply_dropout(i, rnn_out)
        return rnn_out, self.cell_state  # N, L, F


class Decoder(nn.Module):
    def __init__(self, n_features, hidden_dim, rnn_layer: nn.Module = nn.GRU, num_rnn_layers: int = 1, dropout=0,
                 normalization=None, narrow_attn_heads=0):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.n_features = n_features
        self.hidden = None
        self.cell_state = None
        self.dropout = dropout
        self.basic_rnn = rnn_layer(self.n_features, self.hidden_dim, num_layers=1, batch_first=True)
        self.rnns = []
        self.out_normalization = None
        self.hidden_normalization = None
        self.cell_normalization = None
        if num_rnn_layers > 1:
            self.rnns = nn.ModuleList(
                [rnn_layer(self.hidden_dim, self.hidden_dim, num_layers=1, batch_first=True) for _ in
                 range(num_rnn_layers - 1)])
        self.dropouts = []
        if num_rnn_layers > 1 and dropout > 0:
            self.dropouts = nn.ModuleList([nn.Dropout(p=dropout) for _ in range(num_rnn_layers)])
        if normalization == "BatchNormalization":
            self.out_normalization = nn.BatchNorm1d(self.hidden_dim)
            self.hidden_normalization = nn.BatchNorm1d(self.hidden_dim)
            if type(self.basic_rnn) == nn.LSTM:
                self.cell_normalization = nn.BatchNorm1d(self.hidden_dim)
        if normalization == "LayerNormalization":
            self.out_normalization = nn.LayerNorm(self.hidden_dim)
            self.hidden_normalization = nn.LayerNorm(self.hidden_dim)
            if type(self.basic_rnn) == nn.LSTM:
                self.cell_normalization = nn.LayerNorm(self.hidden_dim)
        self.attn_keys = None
        self.attn_values = None
        self.attn = None
        if narrow_attn_heads > 0:
            self.attn = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=narrow_attn_heads, batch_first=True)
            self.linear_keys = nn.Linear(self.hidden_dim, self.hidden_dim)
            self.linear_values = nn.Linear(self.hidden_dim, self.hidden_dim)
        self.regression = nn.Linear(self.hidden_dim * 2 if narrow_attn_heads > 0 else self.hidden_dim, self.n_features)

    def __init_attn_key_values(self, hidden_seq):
        self.attn_keys = self.linear_keys(hidden_seq)
        self.attn_values = self.linear_values(hidden_seq)

    def init_hidden(self, hidden_seq, cell_state=None):
        if self.attn is not None:
            self.__init_attn_key_values(hidden_seq)
        device = next(self.parameters()).device
        # We only need the final hidden state
        hidden_final = hidden_seq[:, -1:]  # N, 1, H
        self.hidden = hidden_final.permute(1, 0, 2).contiguous()  # 1, N, H
        if cell_state is None:
            cell_state_final = torch.zeros(self.hidden.shape).to(device)
        else:
            cell_state_final = cell_state
        self.cell_state = cell_state_final

    def __apply_normalization(self, tensor):
        if type(self.out_normalization) == nn.BatchNorm1d:
            tensor = tensor.permute(0, 2, 1)
            self.hidden = self.hidden.permute(1, 2, 0)
            if self.cell_state is not None:
                self.cell_state = self.cell_state.permute(1, 2, 0)
        output = self.out_normalization(tensor)
        if self.hidden is not None:
            self.hidden = self.hidden_normalization(self.hidden)
        if self.cell_state is not None and self.cell_normalization is not None:
            self.cell_state = self.out_normalization(self.cell_state)  # I can safely replace 0 by 0 if GRU
        if type(self.out_normalization) == nn.BatchNorm1d:
            output = output.permute(0, 2, 1).contiguous()
            self.hidden = self.hidden.permute(2, 0, 1).contiguous()
            if self.cell_state is not None:
                self.cell_state = self.cell_state.permute(2, 0, 1).contiguous()
        return output

    def __apply_dropout(self, index, tensor):
        output = self.dropouts[index](tensor)
        if self.hidden is not None:
            self.hidden = self.dropouts[index](self.hidden)
        if self.cell_state is not None:
            self.cell_state = self.dropouts[index](self.cell_state)  # I can safely replace 0 by 0 if GRU
        return output

    def forward(self, X):
        if type(self.basic_rnn) == nn.LSTM:
            batch_first_output, (self.hidden, self.cell_state) = self.basic_rnn(X, (self.hidden, self.cell_state))
        else:
            batch_first_output, self.hidden = self.basic_rnn(X, self.hidden)
        if self.out_normalization is not None:
            batch_first_output = self.__apply_normalization(batch_first_output)
        if len(self.dropouts) > 0 and self.training:
            batch_first_output = self.__apply_dropout(0, batch_first_output)
        for i, rnn in enumerate(self.rnns, start=1):
            if type(rnn) == nn.LSTM:
                batch_first_output, (self.hidden, self.cell_state) = rnn(batch_first_output,
                                                                         (self.hidden, self.cell_state))
            else:
                batch_first_output, self.hidden = rnn(batch_first_output, self.hidden)
            if self.out_normalization is not None:
                batch_first_output = self.__apply_normalization(batch_first_output)
            if len(self.dropouts) > 0 and self.training:
                batch_first_output = self.__apply_dropout(i, batch_first_output)
        if self.attn is not None:
            attn_output, attn_output_weights = self.attn(batch_first_output, self.attn_keys, self.attn_values)
            batch_first_output = torch.cat([attn_output, batch_first_output], axis=-1)
        # last_output = batch_first_output[:, -1:]
        # out = self.regression(last_output)
        out = self.regression(batch_first_output)

        # N, 1, F
        # return out.view(-1, 1, self.n_features)
        return out
