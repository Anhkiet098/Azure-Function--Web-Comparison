#function_app.py

import os
import subprocess
import sys
import logging

# Thiết lập logging
logging.basicConfig(level=logging.INFO)

import logging
import asyncio
import json
import re
from azure.storage.blob import BlobServiceClient
from playwright.async_api import async_playwright
from PIL import Image
import imagehash
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential
import psycopg2
import os
import azure.functions as func
import time

# Khởi tạo Function App
app = func.FunctionApp()

# Thông tin cấu hình từ biến môi trường
endpoint = os.environ["AZURE_AI_ENDPOINT"]
model_name = os.environ["AZURE_AI_MODEL_NAME"]
credential = AzureKeyCredential(os.environ["AZURE_AI_CREDENTIAL"])
ADMIN_STORAGE_CONN_STR = os.environ["ADMIN_STORAGE_CONN_STR"]
CHALLENGER_STORAGE_CONN_STR = os.environ["CHALLENGER_STORAGE_CONN_STR"]
IMAGE_STORAGE_CONN_STR = os.environ["IMAGE_STORAGE_CONN_STR"]
DB_PARAMS = json.loads(os.environ["DB_PARAMS"])

async def render_html_to_image(html_content, css_content, output_file="output.png"):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        full_html = f"""
        <html>
        <head>
            <style>{css_content}</style>
        </head>
        {html_content}
        </html>
        """
        await page.set_content(full_html)
        await page.screenshot(path=output_file, full_page=True)
        await browser.close()

def compare_images(image1_path, image2_path):
    img1 = Image.open(image1_path)
    img2 = Image.open(image2_path)
    hash1 = imagehash.phash(img1)
    hash2 = imagehash.phash(img2)
    similarity = 1 - (hash1 - hash2) / (len(hash1.hash) ** 2)
    return similarity

def analyze_with_deepseek(image_similarity, original_html, original_css, participant_html, participant_css):
    client = ChatCompletionsClient(
        endpoint=endpoint,
        credential=credential,
        # Tăng timeout lên 180 giây (3 phút)
        timeout=180
    )
    system_message = SystemMessage(content="You are an expert web developer and code reviewer. Your task is to compare two versions of a web page: the original and a participant's submission. You will be given an image similarity percentage, along with separate HTML and CSS files for both versions. Analyze the differences in HTML structure and CSS styling, then combine this with the image similarity to provide an overall similarity percentage. The overall percentage should reflect both visual similarity (from images) and code similarity (from HTML/CSS), with reasonable weighting you determine.")
    user_message = UserMessage(content=f"""
Here is the data for comparison:
- **Image Similarity**: {image_similarity * 100:.2f}%
- **Original HTML**:\n{original_html}
- **Original CSS**:\n{original_css}
- **Participant HTML**:\n{participant_html}
- **Participant CSS**:\n{participant_css}

Perform a quick similarity analysis and return the result **strictly in JSON format only** (no additional text, no <think> tags, no explanations outside the JSON):
{{
    "similarity_percentages": {{
      "visual_similarity": <visual_similarity_value>,
      "code_similarity": <code_similarity_value>,
      "final_combined_similarity": <final_combined_similarity_value>
    }},
    "deepseek_analysis": "<brief analysis in markdown>",
    "improvement_suggestions": "<brief suggestions in markdown>",
    "overall_similarity_percentage": "<brief overall analysis in markdown>",
    "weighting_rationale": "<brief weighting rationale in markdown>"
}}
""")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.complete(messages=[system_message, user_message], max_tokens=4096, model=model_name)
            deepseek_result = response.choices[0].message.content
            logging.info(f"DeepSeek raw response: {deepseek_result}")
            
            # Trích xuất JSON từ phản hồi
            json_match = re.search(r'\{(?:[^{}]|\{[^{}]*\})*\}', deepseek_result)
            if json_match:
                json_str = json_match.group(0)
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError as e:
                    logging.error(f"Failed to parse extracted JSON: {json_str}. Error: {e}")
                    raise ValueError(f"Invalid JSON format after extraction: {json_str}")
            else:
                logging.error(f"No valid JSON found in DeepSeek response: {deepseek_result}")
                raise ValueError(f"DeepSeek did not return a valid JSON response: {deepseek_result}")
        except Exception as e:
            if "Timeout" in str(e) and attempt < max_retries - 1:
                logging.warning(f"Timeout on attempt {attempt + 1}/{max_retries}. Retrying...")
                time.sleep(5)  # Chờ 5 giây trước khi thử lại
                continue
            raise e

def save_to_postgres(json_result, challenge_id, solution_id):
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tbl_similarity_analysis (
            challenge_id, solution_id, visual_similarity, code_similarity, 
            final_combined_similarity, deepseek_analysis, improvement_suggestions, 
            overall_similarity_percentage, weighting_rationale
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        challenge_id, solution_id,
        json_result["similarity_percentages"]["visual_similarity"],
        json_result["similarity_percentages"]["code_similarity"],
        json_result["similarity_percentages"]["final_combined_similarity"],
        json_result["deepseek_analysis"],
        json_result["improvement_suggestions"],
        json_result["overall_similarity_percentage"],
        json_result["weighting_rationale"]
    ))
    conn.commit()
    cur.close()
    conn.close()

def upload_images_to_blob(container_name):
    image_blob_service_client = BlobServiceClient.from_connection_string(IMAGE_STORAGE_CONN_STR)
    container_client = image_blob_service_client.get_container_client(container_name)
    try:
        container_client.create_container()
    except Exception as e:
        if "ContainerAlreadyExists" not in str(e):
            raise
    for img_file in ["original.png", "participant.png"]:
        with open(img_file, "rb") as data:
            container_client.upload_blob(name=img_file, data=data, overwrite=True)

@app.function_name(name="CompareFiles")
@app.route(route="CompareFiles", auth_level=func.AuthLevel.ANONYMOUS)
async def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    challenge_id = req.params.get('challengeid')
    solution_id = req.params.get('solutionid')

    if not challenge_id or not solution_id:
        return func.HttpResponse(
            json.dumps({"error": "Please pass challengeid and solutionid on the query string"}),
            status_code=400,
            mimetype="application/json"
        )

    try:
        # Xây dựng tên container
        admin_container_name = challenge_id
        challenger_container_name = f"{challenge_id}-{solution_id}"

        # Kết nối tới Blob Storage
        admin_blob_service = BlobServiceClient.from_connection_string(ADMIN_STORAGE_CONN_STR)
        challenger_blob_service = BlobServiceClient.from_connection_string(CHALLENGER_STORAGE_CONN_STR)

        # Lấy file từ adminfilesupload
        admin_container_client = admin_blob_service.get_container_client(admin_container_name)
        original_html = admin_container_client.download_blob("index.html").readall().decode("utf-8")
        original_css = admin_container_client.download_blob("styles.css").readall().decode("utf-8")

        # Lấy file từ challengerfileupload
        challenger_container_client = challenger_blob_service.get_container_client(challenger_container_name)
        participant_html = challenger_container_client.download_blob("index.html").readall().decode("utf-8")
        participant_css = challenger_container_client.download_blob("styles.css").readall().decode("utf-8")

        # Chạy quy trình phân tích
        await render_html_to_image(original_html, original_css, "original.png")
        await render_html_to_image(participant_html, participant_css, "participant.png")
        image_similarity = compare_images("original.png", "participant.png")
        json_result = analyze_with_deepseek(image_similarity, original_html, original_css, participant_html, participant_css)

        # Lưu kết quả vào PostgreSQL
        save_to_postgres(json_result, challenge_id, solution_id)

        # Lưu ảnh vào imagechallengerfile
        image_container_name = f"image-{challenger_container_name}"
        upload_images_to_blob(image_container_name)

        # Tạo response JSON chi tiết    
        response_data = {
            "status": "success",
            "message": "Processing completed successfully",
            "challenge_id": challenge_id,
            "solution_id": solution_id,
            "analysis_result": json_result,
            "image_container": image_container_name,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        }

        return func.HttpResponse(
            json.dumps(response_data, ensure_ascii=False),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        logging.error(f"Error processing request: {e}")
        error_response = {
            "status": "error",
            "message": f"An error occurred: {str(e)}",
            "challenge_id": challenge_id,
            "solution_id": solution_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        }
        return func.HttpResponse(
            json.dumps(error_response, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )






    