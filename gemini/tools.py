import settings
from settings import logger, client
from google.genai import types
from PIL import Image
from io import BytesIO
import base64


async def generate_image(prompt: str) -> bytes:
    """Generate an image with the given prompt using Gemini.
    
    Args:
        prompt (str): The prompt to generate the image with.
    
    Returns:
        bytes: The image bytes.
    """
    logger.info(f"tool.generate_image: Generating image with prompt: {prompt}")
    response = client.models.generate_content(
        model="gemini-2.0-flash-exp",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=['Text', 'Image']
        )
    )

    image_bytes = None
    for part in response.candidates[0].content.parts:
        if part.text is not None:
            logger.info(f"Text response: {part.text}")
        elif part.inline_data is not None:
            image = Image.open(BytesIO(part.inline_data.data))
            image.save('gemini-generated-image.png')
            image_bytes = part.inline_data.data
    
    if not image_bytes:
        raise ValueError("No image data received in response")
    
    return image_bytes
