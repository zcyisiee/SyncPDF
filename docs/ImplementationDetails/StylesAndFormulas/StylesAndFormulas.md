# Styles and Formulas Processing

> [!NOTE]
> This documentation may contain AI-generated content. While we strive for accuracy, there might be inaccuracies. Please report any issues via:
>
> - [GitHub Issues](https://github.com/funstory-ai/yadt/issues)
> - Community contribution (PRs welcome!)

## Background

After paragraph finding, we need to identify formulas and text styles within each paragraph. This step is crucial for maintaining mathematical expressions and text formatting during translation.

## Goal

1. Identify and preserve mathematical formulas
2. Detect and maintain consistent text styles
3. Handle special cases like subscripts and superscripts
4. Calculate proper offsets for formula positioning

## Specific Implementation

The processing consists of several main steps:

### Step 1: Formula Detection

1. Identify formula characters based on:
   - Formula-specific fonts
   - Special Unicode characters
   - Vertical text
   - Corner marks (subscripts/superscripts)

2. Group consecutive formula characters into formula units

### Step 2: Formula Processing

1. Process comma-containing formulas:
   - Split complex formulas at commas when appropriate
   - Preserve brackets and their contents
   - Convert simple number-only formulas to regular text

2. Merge overlapping formulas:
   - Handle cases where subscripts/superscripts are detected as separate formulas
   - Maintain proper character ordering

### Step 3: Style Analysis

1. Calculate base style for each paragraph:
   - Find common style attributes across all text
   - Handle font variations
   - Process graphic states

2. Group characters with identical styles:
   - Font properties
   - Size properties
   - Graphic state properties

### Step 4: Position Calculation

1. Calculate formula offsets:
   - Compute x-offset relative to surrounding text
   - Compute y-offset for proper vertical alignment
   - Handle line spacing variations

## Additional Features

1. Font mapping:
   - Maps different fonts to standard ones
   - Special handling for formula fonts

2. Style inheritance:
   - Maintains style hierarchy
   - Handles partial style overrides

3. Formula classification:
   - Distinguishes between translatable and non-translatable formulas
   - Special handling for numeric formulas with commas

## Limitations

1. Formula detection relies on font and character patterns

2. May not handle all types of mathematical notations

3. Complex subscript/superscript combinations might be misidentified

4. Limited support for vertical formulas

## Configuration Options

The formula and style processing can be customized through `TranslationConfig`:

1. `formular_font_pattern`: Regex pattern for identifying formula fonts
2. `formular_char_pattern`: Regex pattern for identifying formula characters 