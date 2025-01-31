# PDF Creation

> [!NOTE]
> This documentation may contain AI-generated content. While we strive for accuracy, there might be inaccuracies. Please report any issues via:
>
> - [GitHub Issues](https://github.com/funstory-ai/yadt/issues)
> - Community contribution (PRs welcome!)

## Background

After translation and typesetting, we need to create the final PDF document that preserves all the formatting, styles, and layout of the original document while containing the translated text. The PDF creation process handles this final step.

## Goal

1. Create a new PDF document with translated content
2. Preserve all original formatting and styles
3. Support both monolingual and dual-language output
4. Maintain font consistency and character encoding
5. Optimize the output file size and performance

## Specific Implementation

The PDF creation process consists of several key steps:

### Step 1: Font Management

1. Font initialization:
   - Add required fonts to the document
   - Map font identifiers
   - Handle font encoding lengths

2. Font availability checking:
   - Check available fonts for each page
   - Handle XObject font requirements
   - Manage font resources

3. Font subsetting:
   - Optimize font usage
   - Reduce file size
   - Maintain character support

### Step 2: Content Rendering

1. Character processing:
   - Handle individual characters
   - Process character encodings
   - Manage character positioning

2. Graphics state handling:
   - Process color spaces
   - Handle transparency
   - Manage graphic state instructions

3. XObject management:
   - Process form XObjects
   - Handle drawing operations
   - Maintain XObject hierarchy

### Step 3: Document Assembly

1. Page construction:
   - Build page content
   - Process page resources
   - Handle page boundaries

2. Content stream creation:
   - Generate drawing operations
   - Handle text positioning
   - Manage content streams

3. Resource management:
   - Handle font resources
   - Manage XObject resources
   - Process graphic states

### Step 4: Output Generation

1. Monolingual output:
   - Create translated-only PDF
   - Optimize file size
   - Apply compression

2. Dual-language output:
   - Combine original and translated pages
   - Handle page ordering
   - Maintain document structure

3. File optimization:
   - Apply garbage collection
   - Enable compression
   - Optimize for linear reading

## Additional Features

1. Font handling:
   - Support for CID fonts
   - Font subsetting
   - Font resource management

2. Document optimization:
   - File size reduction
   - Performance optimization
   - Resource cleanup

3. Debug support:
   - Decompressed output
   - Debug information
   - Progress tracking

## Limitations

1. Font support:
   - Limited to available font formats
   - Font subsetting restrictions
   - Character encoding constraints

2. File size:
   - Dual-language output increases size
   - Font embedding impact
   - Resource duplication

3. Performance considerations:
   - Processing time for large documents
   - Memory usage during creation
   - Optimization overhead

## Configuration Options

The PDF creation process can be customized through `TranslationConfig`:

1. Output options:
   - `no_mono`: Disable monolingual output
   - `no_dual`: Disable dual-language output
   - Output file naming patterns

2. Optimization settings:
   - Compression options
   - Garbage collection
   - Font subsetting

3. Debug options:
   - Debug mode
   - Decompressed output
   - Progress tracking 