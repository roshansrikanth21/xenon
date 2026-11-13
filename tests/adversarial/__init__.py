"""Adversarial corpus: unit-level attacks on Xenon's safety core.

Each test plays one attacker from the threat model (a remote sender, a web
page author, a model that hallucinates) against the code that decides risk.
The live, end-to-end variants run in `xenon selftest`.
"""
