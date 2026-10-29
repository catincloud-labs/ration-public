"""The conformance suite.

A suite that cannot reject is not a suite. Every property in `conformance/`
ships alongside an implementation in `violators.py` that breaks exactly that
property and is expected to fail it, and `test_the_suite_discriminates.py`
checks that the pairing is complete in both directions.
"""
