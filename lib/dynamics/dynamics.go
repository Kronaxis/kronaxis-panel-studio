// DYNAMICS-8 Personality Framework - Go Reference Implementation
// Copyright (c) 2026 Kronaxis Limited. All rights reserved.
// Licensed under BSL 1.1. See LICENSE file.
//
// Created by Jason Duke, Kronaxis Limited
// https://kronaxis.co.uk/dynamics
// Patent: UK Patent Application C (GB 2605150.8)
package dynamics

import (
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"strings"
)

// DimensionInfo describes a single DYNAMICS-8 dimension.
type DimensionInfo struct {
	Name        string
	Description string
	Facets      [4]string
}

// Dimensions maps each letter code to its definition.
var Dimensions = map[string]DimensionInfo{
	"D": {Name: "Discipline", Description: "Self-regulation, planning, routine adherence",
		Facets: [4]string{"Organisation", "Diligence", "Perfectionism", "Prudence"}},
	"Y": {Name: "Yielding", Description: "Agreeableness, compliance, conflict avoidance",
		Facets: [4]string{"Patience", "Tolerance", "Flexibility", "Persuadability"}},
	"N": {Name: "Novelty", Description: "Openness to new experiences, curiosity",
		Facets: [4]string{"Aesthetic sense", "Inquisitiveness", "Creativity", "Unconventionality"}},
	"A": {Name: "Acuity", Description: "Digital fluency, platform nativeness, research tendency",
		Facets: [4]string{"Platform nativeness", "Content creation", "Privacy awareness", "Tech adoption"}},
	"M": {Name: "Mercuriality", Description: "Emotional volatility, anxiety proneness",
		Facets: [4]string{"Anxiety", "Emotional reactivity", "Sentimentality", "Dependence"}},
	"I": {Name: "Impulsivity", Description: "Decision speed, spontaneous action",
		Facets: [4]string{"Delay discounting", "Sensation seeking", "Snap decision", "Boredom susceptibility"}},
	"C": {Name: "Candour", Description: "Honesty, ethical concern, transparency",
		Facets: [4]string{"Sincerity", "Fairness", "Modesty", "Materialism (inverse)"}},
	"S": {Name: "Sociability", Description: "Social engagement, group orientation",
		Facets: [4]string{"Social boldness", "Liveliness", "Social esteem", "Gregariousness"}},
}

// DimensionOrder is the canonical ordering of dimension letters.
var DimensionOrder = [8]string{"D", "Y", "N", "A", "M", "I", "C", "S"}

// Profile holds a DYNAMICS-8 personality profile.
type Profile struct {
	D      float64                `json:"D"`
	Y      float64                `json:"Y"`
	N      float64                `json:"N"`
	A      float64                `json:"A"`
	M      float64                `json:"M"`
	I      float64                `json:"I"`
	C      float64                `json:"C"`
	S      float64                `json:"S"`
	Facets map[string][4]float64  `json:"facets,omitempty"`
}

// score returns the value for a given dimension letter.
func (p Profile) score(dim string) float64 {
	switch dim {
	case "D": return p.D
	case "Y": return p.Y
	case "N": return p.N
	case "A": return p.A
	case "M": return p.M
	case "I": return p.I
	case "C": return p.C
	case "S": return p.S
	}
	return 0
}

// Validate checks that all scores fall within [0.0, 1.0] and facets (if present)
// have exactly 4 values each within range.
func (p Profile) Validate() error {
	for _, dim := range DimensionOrder {
		v := p.score(dim)
		if v < 0.0 || v > 1.0 {
			return fmt.Errorf("dimension %s out of range: %.4f", dim, v)
		}
	}
	for dim, facets := range p.Facets {
		if _, ok := Dimensions[dim]; !ok {
			return fmt.Errorf("unknown facet dimension: %s", dim)
		}
		for i, f := range facets {
			if f < 0.0 || f > 1.0 {
				return fmt.Errorf("facet %s[%d] out of range: %.4f", dim, i, f)
			}
		}
	}
	return nil
}

// DimensionLabel returns a qualitative label for the given dimension.
func (p Profile) DimensionLabel(dim string) string {
	v := p.score(dim)
	switch {
	case v < 0.2:
		return "very low"
	case v < 0.4:
		return "low"
	case v < 0.6:
		return "moderate"
	case v < 0.8:
		return "high"
	default:
		return "very high"
	}
}

// Summary returns a human-readable description of the profile.
func (p Profile) Summary() string {
	parts := make([]string, 0, 8)
	for _, dim := range DimensionOrder {
		info := Dimensions[dim]
		label := p.DimensionLabel(dim)
		parts = append(parts, fmt.Sprintf("%s %s (%.2f)", strings.Title(label), info.Name, p.score(dim)))
	}
	return strings.Join(parts, ", ")
}

// GenerateProfile creates a random profile with uniform scores.
func GenerateProfile() Profile {
	return Profile{
		D: rand.Float64(),
		Y: rand.Float64(),
		N: rand.Float64(),
		A: rand.Float64(),
		M: rand.Float64(),
		I: rand.Float64(),
		C: rand.Float64(),
		S: rand.Float64(),
	}
}

// CompatibilityScore returns a 0.0-1.0 similarity score between two profiles.
// Uses inverted normalised Euclidean distance across all 8 dimensions.
func CompatibilityScore(a, b Profile) float64 {
	sumSq := 0.0
	for _, dim := range DimensionOrder {
		diff := a.score(dim) - b.score(dim)
		sumSq += diff * diff
	}
	dist := math.Sqrt(sumSq / 8.0)
	return 1.0 - dist
}

// DeriveIncomeBand returns a predicted income band based on D and N.
func DeriveIncomeBand(p Profile) string {
	v := p.D*0.6 + p.N*0.4
	switch {
	case v < 0.25:
		return "low"
	case v < 0.45:
		return "lower-middle"
	case v < 0.65:
		return "middle"
	case v < 0.82:
		return "upper-middle"
	default:
		return "high"
	}
}

// DeriveSpendingPattern returns a predicted spending pattern based on C, D, and I.
func DeriveSpendingPattern(p Profile) string {
	v := (1.0-p.I)*0.4 + p.D*0.3 + p.C*0.3
	switch {
	case v < 0.25:
		return "impulsive"
	case v < 0.42:
		return "generous"
	case v < 0.58:
		return "moderate"
	case v < 0.75:
		return "careful"
	default:
		return "frugal"
	}
}

// DeriveRiskTolerance returns a predicted risk tolerance based on Y, I, and M (inverse).
func DeriveRiskTolerance(p Profile) string {
	v := p.Y*0.25 + p.I*0.40 + (1.0-p.M)*0.35
	switch {
	case v < 0.25:
		return "very low"
	case v < 0.42:
		return "low"
	case v < 0.58:
		return "moderate"
	case v < 0.75:
		return "high"
	default:
		return "very high"
	}
}

// DerivePoliticalLean returns a value from -1.0 (far left) to 1.0 (far right).
// N pushes left, inverse-C pushes right, Y dampens toward centre.
func DerivePoliticalLean(p Profile) float64 {
	leftPull := p.N * 0.45
	rightPull := (1.0 - p.C) * 0.35
	centreDamp := p.Y * 0.20
	raw := rightPull - leftPull + centreDamp
	if raw < -1.0 {
		return -1.0
	}
	if raw > 1.0 {
		return 1.0
	}
	return raw
}

// ParseProfile unmarshals JSON bytes into a Profile and validates it.
func ParseProfile(data []byte) (Profile, error) {
	var p Profile
	if err := json.Unmarshal(data, &p); err != nil {
		return p, fmt.Errorf("parse profile: %w", err)
	}
	if err := p.Validate(); err != nil {
		return p, fmt.Errorf("invalid profile: %w", err)
	}
	return p, nil
}
