"""Independent data-source connectors for the LARP detector.

Each connector in this package gathers ADDITIVE evidence only, exactly like
detective/pitchbook.py: it returns a list of evidence records and never sets
a claim's tier or the dossier's score. See registry.py for the weighted
source table and sources.github / sources.sec_edgar / sources.wayback /
sources.domain_age for the four connectors implemented so far.

No em dashes anywhere in this package (house rule).
"""
