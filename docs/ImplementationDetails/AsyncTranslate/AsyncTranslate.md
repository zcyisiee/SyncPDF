# Async Translation API

> [!NOTE]
> This documentation may contain AI-generated content. While we strive for accuracy, there might be inaccuracies. Please report any issues via:
>
> - [GitHub Issues](https://github.com/funstory-ai/yadt/issues)
> - Community contribution (PRs welcome!)

## Overview

The `yadt.high_level.async_translate` function provides an asynchronous interface for translating PDF files with real-time progress reporting. This function yields progress events that can be used to update progress bars or other UI elements.

## Usage

```python linenums="1"
async def translate_with_progress():
    config = TranslationConfig(
        input_file="example.pdf",
        translator=your_translator,
        # ... other configuration options
    )
    
    try:
        async for event in async_translate(config):
            if event["type"] == "progress_update":
                print(f"Progress: {event['overall_progress']}%")
            elif event["type"] == "finish":
                result = event["translate_result"]
                print(f"Translation completed: {result.original_pdf_path}")
            elif event["type"] == "error":
                print(f"Error occurred: {event['error']}")
                break
    except asyncio.CancelledError:
        print("Translation was cancelled")
    except KeyboardInterrupt:
        print("Translation was interrupted")
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

Emitted periodically during translation (controlled by report_interval, default 0.1s):

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

The translation process can be cancelled in several ways:

1. By raising a `CancelledError` (e.g., when using `asyncio.Task.cancel()`)
2. Through `KeyboardInterrupt` (e.g., when user presses Ctrl+C)
3. By calling `translation_config.cancel_translation()` method

Example of programmatic cancellation:

```python linenums="1"
async def translate_with_cancellation():
    config = TranslationConfig(
        input_file="example.pdf",
        translator=your_translator,
        # ... other configuration options
    )
    
    try:
        # Start translation in another task
        translation_task = asyncio.create_task(process_translation(config))
        
        # Simulate some condition that requires cancellation
        await asyncio.sleep(5)
        config.cancel_translation()  # This will trigger cancellation
        
        await translation_task  # Wait for the task to finish
    except asyncio.CancelledError:
        print("Translation was cancelled")

async def process_translation(config):
    async for event in async_translate(config):
        if event["type"] == "error":
            if isinstance(event["error"], asyncio.CancelledError):
                print("Translation was cancelled")
                break
            print(f"Error occurred: {event['error']}")
            break
        # ... handle other events ...
```

When cancelled:
- The function will log the cancellation reason
- All resources will be cleaned up properly
- Any ongoing translation tasks will be stopped
- A final error event with `CancelledError` will be emitted
- The function will exit gracefully

## Error Handling

Any errors during translation will be:
1. Logged with full traceback (if debug mode is enabled)
2. Reported through an error event
3. Cause the event stream to stop after the error event
4. Clean up resources properly before exiting

It's recommended to handle these events appropriately in your application to provide feedback to users. The example in the Usage section shows a basic error handling pattern. 