from typing import Dict, Iterator, Optional
from textwrap import dedent
from agno.workflow import RunEvent, RunResponse, Workflow
from pydantic import BaseModel, Field
from agent import CSVAgent, EmailAgent, TextAgent
from agno.storage.sqlite import SqliteStorage
from agno.utils.log import logger
from agno.utils.pprint import pprint_run_response
import time
import os
from fastapi import UploadFile

class CustomerData(BaseModel):
    name: str = Field(..., description="Customer's name")
    email: str = Field(..., description="Customer's email address")
    description: str = Field(..., description="Personal description of the customer")

# ...existing imports...



class MarketingEmailWorkflow(Workflow):
    """Workflow for sending personalized marketing emails to customers based on CSV data."""

    description: str = dedent("""
    An intelligent marketing email workflow that processes customer data from a CSV file
    and sends personalized marketing emails. The workflow combines data processing with
    personalized content generation to create targeted marketing campaigns.
    """)

    def __init__(
        self,
        session_id: str,
        csv_file: str,
        sender_email: str,
        sender_name: str,
        sender_passkey: str,
        model: str = "gemini",
        *args,
        **kwargs
    ):
        super().__init__(session_id=session_id, *args, **kwargs)
        # Handle file path resolution within data directory
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        if not os.path.isabs(csv_file):
            self.csv_file_path = os.path.join(data_dir, csv_file)
        else:
            self.csv_file_path = csv_file

        self.sender_email = sender_email
        self.sender_name = sender_name
        self.sender_passkey = sender_passkey
        self.model = model

        # Initialize CSV Agent for reading customer data
        self.csv_agent = CSVAgent(model=self.model, file_path=self.csv_file_path)

    def run(
        self,
        company_name: str,
        product_description: str,
        use_cached_results: bool = True,
        max_retries: int = 3,
        retry_delay: int = 5
    ) -> Iterator[RunResponse]:
        logger.info(f"Starting marketing email campaign for {company_name}")

        # Input validation
        if not company_name or not product_description:
            raise ValueError("Company name and product description are required")

        # Check cache for previously sent emails
        if use_cached_results:
            cached_results = self.get_cached_results(company_name)
            if cached_results:
                yield RunResponse(
                    content=cached_results,
                    event=RunEvent.workflow_completed
                )
                return

        try:
            # Get customer data from CSV with proper error handling
            customer_query = "SELECT * FROM sample_marketing"
            retry_count = 0
            csv_response = None
            
            while retry_count < max_retries:
                try:
                    csv_response = self.csv_agent.run_agent(customer_query)
                    if csv_response and len(csv_response.strip()) > 0:
                        break
                    raise Exception("Empty response from CSV agent")
                except Exception as e:
                    retry_count += 1
                    logger.warning(f"Attempt {retry_count} failed: {str(e)}")
                    if retry_count < max_retries:
                        time.sleep(retry_delay)
                    else:
                        raise Exception(f"Failed to fetch customer data after {max_retries} attempts: {str(e)}")
            
            if not csv_response:
                raise Exception("No customer data received from CSV")
                
            # Process each customer and send personalized email with delay between sends
            results = []
            for customer_data in self.parse_csv_response(csv_response):
                logger.debug(f"Processing customer: {customer_data.email}")

                # Create email content based on customer description
                email_content = self.generate_email_content(
                    customer_data,
                    company_name,
                    product_description
                )
                logger.debug(f"Generated email content for {customer_data.email}: {email_content[:100]}...")

                # Initialize EmailAgent for this customer
                email_agent = EmailAgent(
                    model=self.model,
                    receiver_email=customer_data.email,
                    sender_email=self.sender_email,
                    sender_name=self.sender_name,
                    sender_passkey=self.sender_passkey
                )

                retry_count = 0
                while retry_count < max_retries:
                    try:
                        logger.debug(f"Sending email to {customer_data.email}, attempt {retry_count+1}")
                        # Send personalized email with exponential backoff
                        email_response = email_agent.run_agent(email_content)
                        logger.debug(f"Email sent to {customer_data.email} with response: {email_response}")
                        results.append({
                            "customer": customer_data.model_dump(),
                            "email_status": "success",
                            "response": email_response
                        })
                        # Add delay between sends with exponential backoff
                        time.sleep(retry_delay * (2 ** retry_count))
                        break
                    except Exception as e:
                        retry_count += 1
                        logger.warning(f"Attempt {retry_count} failed for {customer_data.email}: {str(e)}")
                        if retry_count < max_retries:
                            time.sleep(retry_delay * (2 ** retry_count))
                        else:
                            logger.error(f"Failed to send email to {customer_data.email} after {max_retries} attempts")
                            results.append({
                                "customer": customer_data.model_dump(),
                                "email_status": "failed",
                                "error": f"Failed after {max_retries} attempts: {str(e)}"
                            })

            # Cache the results
            self.add_results_to_cache(company_name, results)

            # Return final response
            yield RunResponse(
                content=f"Successfully sent {len(results)} personalized marketing emails",
                event=RunEvent.workflow_completed
            )

        except Exception as e:
            logger.error(f"Error in marketing workflow: {str(e)}")
            yield RunResponse(
                content=f"Error: {str(e)}",
                event=RunEvent.workflow_completed
            )

    def parse_csv_response(self, csv_response: str) -> Iterator[CustomerData]:
        """Parse the CSV agent response and yield CustomerData objects."""
        try:
            logger.debug("Parsing CSV response: %s", csv_response)
            # Skip empty responses
            if not csv_response or not csv_response.strip():
                logger.warning("Empty CSV response received")
                return

            # Split the response into lines and clean up
            lines = [line.strip() for line in csv_response.strip().split('\n') if line.strip()]
            logger.debug("CSV response split into %d lines", len(lines))
            if not lines:
                logger.warning("No valid data found in CSV response")
                return

            # Find the actual data rows (skip markdown formatting)
            data_rows = []
            for line in lines:
                # Skip markdown table formatting and empty lines
                if line.startswith('|') and not line.replace('|', '').replace('-', '').strip() == '':
                    cleaned_line = line.strip('|').strip()
                    if cleaned_line and not cleaned_line.startswith('-'):
                        data_rows.append(cleaned_line)
            logger.debug("Extracted %d data rows", len(data_rows))

            # Process each data row (skipping header)
            for row_str in data_rows[1:]:
                logger.debug("Processing row: %s", row_str)
                try:
                    cells = [cell.strip() for cell in row_str.split('|')]
                    if len(cells) >= 3:
                        name = cells[0]
                        email = cells[1]
                        description = cells[2]
                        if '@' in email:
                            yield CustomerData(
                                name=name,
                                email=email,
                                description=description
                            )
                        else:
                            logger.warning("Invalid email format in row: %s", row_str)
                    else:
                        logger.warning("Skipping invalid row: %s", row_str)
                except Exception as e:
                    logger.error("Error processing row '%s': %s", row_str, str(e))
                    continue
        except Exception as e:
            logger.error("Error parsing CSV response: %s", str(e))
            raise

    def generate_email_content(self, customer: CustomerData, company_name: str, product_description: str) -> str:
        """Generate personalized email content using TextAgent based on customer data."""
        # Initialize TextAgent for content generation
        text_agent = TextAgent(
            model=self.model,
            instructions="You are an expert marketing copywriter. Generate personalized email content that is engaging, professional, and tailored to the recipient's profile."
        )
        
        # Prepare the prompt for content generation
        prompt = f"""Generate a marketing email with the following details:
        - Recipient's Name: {customer.name}
        - Recipient's Professional Background: {customer.description}
        - Company Name: {company_name}
        - Product/Service Description: {product_description}
        -Sender's Name: {self.sender_name}
        
        The email should:
        1. Have an engaging subject line
        2. Be personalized based on the recipient's background
        3. Highlight how our solutions address their specific needs
        4. Include a clear call to action
        5. Maintain a professional yet friendly tone
        
        Format the email with proper structure including subject line, greeting, body, and signature."""
        
        # Generate personalized content
        email_content = text_agent.run_agent(prompt)
        return email_content

    def get_cached_results(self, company_name: str) -> Optional[str]:
        """Retrieve cached results for the company's campaign."""
        return self.session_state.get("email_campaigns", {}).get(company_name)

    def add_results_to_cache(self, company_name: str, results: list):
        """Cache the results of the email campaign."""
        self.session_state.setdefault("email_campaigns", {})
        self.session_state["email_campaigns"][company_name] = results