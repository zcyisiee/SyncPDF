# Intermediate Layer Translator

> [!NOTE]
> This documentation may contain AI-generated content. While we strive for accuracy, there might be inaccuracies. Please report any issues via:
>
> - [GitHub Issues](https://github.com/funstory-ai/yadt/issues)
> - Community contribution (PRs welcome!)

## Background

After formula and style processing, we need to translate the document while preserving all formatting, formulas, and styles. The intermediate layer translator handles this complex task by using placeholders and style preservation techniques.

## Goal

1. Translate text while preserving document structure
2. Maintain formulas and special formatting
3. Handle rich text with different styles
4. Support concurrent translation for better performance

## Specific Implementation

The translation process consists of several key steps:

### Step 1: Translation Preparation

1. Process paragraphs:
   - Skip vertical text
   - Handle single-component paragraphs directly
   - Process multi-component paragraphs with placeholders

2. Create placeholders:
   - Formula placeholders for mathematical expressions
   - Rich text placeholders for styled text
   - Ensure placeholder uniqueness within each paragraph

### Step 2: Translation Input Creation

1. Analyze paragraph components:
   - Regular text components
   - Formula components
   - Styled text components

2. Handle special cases:
   - Skip pure formula paragraphs
   - Preserve original text when style matches base style
   - Handle font mapping cases

### Step 3: Translation Execution

1. Concurrent translation:
   - Use thread pool for parallel processing
   - Control QPS (Queries Per Second)
   - Track translation progress

2. Translation tracking:
   - Record original text
   - Record translated text
   - Save tracking information for debugging

### Step 4: Translation Output Processing

1. Parse translated text:
   - Extract text between placeholders
   - Restore formulas at placeholder positions
   - Restore rich text with original styles

2. Create new paragraph components:
   - Maintain style information
   - Preserve formula positioning
   - Handle empty text segments

## Additional Features

1. Style preservation:
   - Maintains original text styles
   - Handles font size variations
   - Preserves formatting attributes

2. Formula handling:
   - Preserves formula integrity
   - Maintains formula positioning
   - Supports complex mathematical expressions

3. Debug support:
   - Translation tracking
   - JSON output for debugging
   - Detailed logging

## Limitations

1. Vertical text is not supported

2. Complex nested styles might not be perfectly preserved

3. Placeholder conflicts could occur in rare cases

4. Translation quality depends on external translation engine

## Configuration Options

The translation process can be customized through `TranslationConfig`:

1. `qps`: Maximum queries per second for translation
2. `debug`: Enable/disable debug mode and tracking
3. Translation engine specific settings 