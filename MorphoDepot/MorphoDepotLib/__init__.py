"""MorphoDepot implementation package.

The Slicer module factory requires the four classes (MorphoDepot,
MorphoDepotWidget, MorphoDepotLogic, MorphoDepotTest) to live in MorphoDepot.py.
Everything else is split into this package by domain; the main file keeps those
four thin and inherits behavior from per-domain mixins here. See
docs/refactor-and-test-plan.md.
"""
