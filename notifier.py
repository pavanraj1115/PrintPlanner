import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_RECIPIENT

def send_material_shortage_alert(build_id, material_name, required_grams, available_grams, sales_orders):
    """
    Sends an automated email notification requesting purchase of new filament.
    """
    subject = f"⚠️ [ACTION REQUIRED] Material Shortage Alert for Build #{build_id} ({material_name})"
    
    so_list_str = ", ".join(sales_orders)
    shortage_grams = required_grams - available_grams
    
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #222; line-height: 1.6;">
        <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 24px; max-width: 600px;">
          <h2 style="color: #dc2626; margin-top: 0;">⚠️ Filament Stock Insufficient for Production</h2>
          <p>The <strong>ProtoValue3D Build Planner</strong> has identified a material shortage while preparing <strong>Build #{build_id}</strong>.</p>
          
          <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
            <tr style="border-bottom: 1px solid #cbd5e1;">
              <td style="padding: 8px; font-weight: bold;">Material Required:</td>
              <td style="padding: 8px; color: #0284c7; font-weight: bold;">{material_name}</td>
            </tr>
            <tr style="border-bottom: 1px solid #cbd5e1;">
              <td style="padding: 8px; font-weight: bold;">Associated Sales Orders:</td>
              <td style="padding: 8px;">{so_list_str}</td>
            </tr>
            <tr style="border-bottom: 1px solid #cbd5e1;">
              <td style="padding: 8px; font-weight: bold;">Estimated Build Weight:</td>
              <td style="padding: 8px;">{required_grams:.1f} g</td>
            </tr>
            <tr style="border-bottom: 1px solid #cbd5e1;">
              <td style="padding: 8px; font-weight: bold;">Available in Stores:</td>
              <td style="padding: 8px; color: #dc2626;">{available_grams:.1f} g</td>
            </tr>
            <tr style="background: #fef2f2; font-weight: bold;">
              <td style="padding: 8px; color: #991b1b;">Net Shortage:</td>
              <td style="padding: 8px; color: #991b1b;">{shortage_grams:.1f} g (approx {shortage_grams/1000.0:.1f} spools)</td>
            </tr>
          </table>

          <div style="background: #fffbeb; border-left: 4px solid #f59e0b; padding: 12px; margin: 16px 0;">
            <strong>Planner Action:</strong> This build has <em>NOT</em> been posted to ERPNext Print Build. It is currently held in your local Slicer Queue (<code>queued_builds/</code>).
          </div>

          <p style="margin-top: 20px;">Please issue a Purchase Order in ERPNext to replenish this material stock.</p>
          <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
          <small style="color: #64748b;">ProtoValue3D Automated Print Farm System</small>
        </div>
      </body>
    </html>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"ProtoValue3D Print Planner <{SMTP_USER}>"
        msg["To"] = ALERT_EMAIL_RECIPIENT
        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, ALERT_EMAIL_RECIPIENT, msg.as_string())
            
        print(f"[Notifier] Dispatched material shortage email to {ALERT_EMAIL_RECIPIENT} for {material_name}.")
        return True
    except Exception as e:
        print(f"[Notifier] Failed to send email alert: {e}")
        return False
