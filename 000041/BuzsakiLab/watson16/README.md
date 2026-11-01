# Watson et al. 2016: neocortical sleep (DANDI:000041)

Notebooks for [DANDI:000041](https://dandiarchive.org/dandiset/000041), the recordings of

> Watson BO, Levenstein D, Greene JP, Gelinas JN, Buzsáki G (2016).
> Network homeostasis and state dynamics of neocortical sleep. *Neuron* 90(4):839-852.
> https://doi.org/10.1016/j.neuron.2016.03.036

Rats with silicon probes in frontal cortex were recorded in their home cage through hours of natural sleep and
waking. Each file holds the LFP of every probe channel, the theta and gamma phase of the LFP on three reference
channels, the waveforms of every detected spike, the sorted units with putative cell type, and the scored sleep
states.

## Notebooks

- `watson16_alignment.ipynb` streams one session and shows how its data streams relate in time: the sleep states
  against the delta and theta power of the LFP, the spikes of all units against the slow oscillation of the LFP in
  NREM sleep, firing rates by state and cell type, and the units against the spike waveforms of their shank.
