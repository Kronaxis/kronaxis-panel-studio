# DYNAMICS-8 Personality Framework
# Copyright (c) 2026 Kronaxis Limited. All rights reserved.
# Licensed under BSL 1.1. See LICENSE file.
#
# Created by Jason Duke, Kronaxis Limited
# https://kronaxis.co.uk/dynamics

"""DYNAMICS-8: A purpose-built behavioural simulation framework."""

from .dynamics import (
    DIMENSIONS,
    DynamicsProfile,
    generate_profile,
    compatibility_score,
    derive_income_band,
    derive_spending_pattern,
    derive_risk_tolerance,
    derive_political_lean,
)

__all__ = [
    "DIMENSIONS",
    "DynamicsProfile",
    "generate_profile",
    "compatibility_score",
    "derive_income_band",
    "derive_spending_pattern",
    "derive_risk_tolerance",
    "derive_political_lean",
]
