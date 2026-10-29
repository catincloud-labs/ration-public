"""One module per contract property, and one property per module.

The split is not organisational. A property is what the suite reports a verdict
on, so a store expected to fail a property must fail **every** test in that
property's module -- a test that some violator quietly passes would be marked
strict-xfail and turn into a failure, which is the design working rather than an
inconvenience to route around.
"""
