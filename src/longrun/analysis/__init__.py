"""Deterministic training analysis.

Reserved for v1.1, the VDOT plan engine: deriving VDOT from parkrun and race
efforts, and generating training paces and week structures from it.

The package exists now to fix the seam. Anything that turns stored records
into training prescriptions belongs here, computed in Python and testable
against fixed inputs - not in the reporting layer, which only draws, and not
in a model call. When the v1.2 narrator arrives it will consume what this
package returns rather than reasoning about the data itself.

See README.md > The design principle.
"""
