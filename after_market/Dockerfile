FROM public.ecr.aws/lambda/python:3.13
ENV HOME=/tmp
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt --target "${LAMBDA_TASK_ROOT}"
COPY app.py ${LAMBDA_TASK_ROOT}/
CMD ["app.lambda_handler"]
