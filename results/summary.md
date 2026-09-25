# Transistor comparator - simulation results

## Characteristics

| Parameter                                      | Value             |
|------------------------------------------------|-------------------|
| Supply VCC                                     | 5 V               |
| Reference V_REF                                | 2.5 V             |
| Switching threshold (OUT = VCC/2)              | 2.4826 V          |
| Offset threshold - V_REF                       | -17.4 mV          |
| Transition width (10 % -> 90 % OUT)            | 6.4 mV            |
| Max. DC gain dOUT/dV_IN                        | 769 V/V           |
| OUT low (V_IN = 0 V)                           | 0.00 mV           |
| OUT high (V_IN = 5 V)                          | 4.907 V           |
| Tail current at V_IN = V_REF                   | 401 µA            |
| Propagation delay LH (pulse, 200 mV overdrive) | 160 ns            |
| Propagation delay HL (pulse, 200 mV overdrive) | 1.33 µs           |
| Rise time OUT 10-90 %                          | 141 ns            |
| Fall time OUT 90-10 %                          | 331 ns            |
| 1 kHz sine: delay rising / falling             | -598 ns / 2.81 µs |
| 1 kHz sine: OUT duty cycle                     | 50.4 %            |

## Threshold as a function of V_REF

| V_REF [V] | Threshold [V] | Offset [mV] | Width [mV] | Gain [V/V] | OUT low [V] | OUT high [V] |
|-----------|---------------|-------------|------------|------------|-------------|--------------|
| 1.00      | 1.242         | +241.7      | 91.1       | 70         | 0.000       | 4.907        |
| 2.50      | 2.483         | -17.4       | 6.4        | 769        | 0.000       | 4.907        |
| 4.00      | 3.934         | -65.7       | 6.4        | 801        | 0.000       | 4.907        |
