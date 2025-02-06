# Async Translation API

## Overview

The `yadt.high_level.async_translate` function provides an asynchronous interface for translating PDF files with real-time progress reporting. This function yields progress events that can be used to update progress bars or other UI elements.

## Usage

```python
async def translate_with_progress():
    config = TranslationConfig(
        input_file="example.pdf",
        translator=your_translator,
        # ... other configuration options
    )
    
    async for event in async_translate(config):
        if event["type"] == "progress_update":
            print(f"Progress: {event['overall_progress']}%")
        elif event["type"] == "finish":
            result = event["translate_result"]
            print(f"Translation completed: {result.original_pdf_path}")
```

## Event Types

The function yields different types of events during the translation process:

### 1. Progress Start Event

Emitted when a translation stage begins:

```python
{
    "type": "progress_start",
    "stage": str,              # Name of the current stage
    "stage_progress": float,   # Always 0.0
    "stage_current": int,      # Current progress count (0)
    "stage_total": int         # Total items to process in this stage
}
```

### 2. Progress Update Event

Emitted periodically during translation:

```python
{
    "type": "progress_update",
    "stage": str,              # Name of the current stage
    "stage_progress": float,   # Progress percentage of current stage (0-100)
    "stage_current": int,      # Current items processed in this stage
    "stage_total": int,        # Total items to process in this stage
    "overall_progress": float  # Overall translation progress (0-100)
}
```

### 3. Progress End Event

Emitted when a stage completes:

```python
{
    "type": "progress_end",
    "stage": str,              # Name of the completed stage
    "stage_progress": float,   # Always 100.0
    "stage_current": int,      # Equal to stage_total
    "stage_total": int,        # Total items processed in this stage
    "overall_progress": float  # Overall translation progress (0-100)
}
```

### 4. Finish Event

Emitted when translation completes successfully:

```python
{
    "type": "finish",
    "translate_result": TranslateResult  # Contains paths to translated files and timing info
}
```

### 5. Error Event

Emitted if an error occurs during translation:

```python
{
    "type": "error",
    "error": str  # Error message
}
```

## Translation Stages

The translation process goes through the following stages in order:

1. ILCreater
2. LayoutParser
3. ParagraphFinder
4. StylesAndFormulas
5. ILTranslator
6. Typesetting
7. FontMapper
8. PDFCreater

Each stage will emit its own set of progress events.

## Cancellation

The translation process can be cancelled by raising a `CancelledError` or `KeyboardInterrupt`. The function will clean up resources and stop gracefully.

## Error Handling

Any errors during translation will be reported through an error event. It's recommended to handle these events appropriately in your application to provide feedback to users. 