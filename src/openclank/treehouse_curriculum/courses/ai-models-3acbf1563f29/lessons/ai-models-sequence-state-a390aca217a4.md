# Trace carried sequence state

Toy rule: h0=0; ht=0.5×h(t−1)+xt.

Input [2,0,0] → states [2,1,0.5]. Each new state depends on the previous state and current input. Retrace for [0,0,2].

This scalar recurrence illustrates order and decaying state. It is not an LSTM or an implementation of learned gates.
