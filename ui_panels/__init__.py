"""HUD side panels kept out of ui.py.

``ui.py`` is the single largest file in the project. Every panel added to it
makes the next one harder to place, so panels live here and ``ui.py`` only
learns how to open them. Import the panel classes from their own module rather
than from this package, so that opening one panel does not construct the
imports of the others.
"""
