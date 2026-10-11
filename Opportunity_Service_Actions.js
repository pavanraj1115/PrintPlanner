frappe.ui.form.on('Opportunity', {
    refresh(frm) {
        // 1. Product Development Project button if type is Product Development
        if (frm.doc.opportunity_type === 'Product Development') {
            if (frm.doc.custom_product_development_project) {
                frm.add_custom_button(__('View Project: ') + frm.doc.custom_product_development_project, () => {
                    frappe.set_route('Form', 'Product Development Project', frm.doc.custom_product_development_project);
                }, __('Project'));
            } else if (!frm.is_new()) {
                frm.add_custom_button(__('Product Development Project'), () => {
                    create_pdp_from_opportunity(frm);
                }, __('Create'));
            }
        }

        // 2. Google Drive Action Buttons
        if (!frm.doc.custom_google_drive_link) {
            let btn = frm.add_custom_button(__('📁 Create Drive Folder'), () => {
                handle_create_drive_folder_click(frm);
            });
            btn.addClass('btn-primary');
        } else {
            frm.add_custom_button(__('📁 Open Drive Folder'), () => {
                window.open(frm.doc.custom_google_drive_link, '_blank');
            });

            frm.add_custom_button(__('Rename Drive to Deal Name'), () => {
                rename_opportunity_drive_folder(frm, true);
            }, __('Actions'));
        }

        // 3. File Details Child Table Grid Button (Only at the files table)
        if (frm.fields_dict['custom_file_details'] && frm.fields_dict['custom_file_details'].grid) {
            frm.fields_dict['custom_file_details'].grid.add_custom_button(__('📥 Fetch Files from Drive'), () => {
                fetch_drive_files_to_child_table(frm);
            });
        }
    },

    onload(frm) {
        if (frm.is_new() && !frm.doc.party_name) {
            if (frm.doc.custom_google_drive_link) {
                frm.set_value('custom_google_drive_link', '');
            }
        }
        sync_opportunity_drive_link(frm);
    },
    party_name(frm) {
        sync_opportunity_drive_link(frm);
    },
    after_save(frm) {
        rename_opportunity_drive_folder(frm, false);
    }
});

function handle_create_drive_folder_click(frm) {
    if (!frm.doc.party_name) {
        frappe.msgprint({
            title: __('Customer / Lead Required'),
            indicator: 'orange',
            message: __('Please select a Customer or Lead first before creating the Google Drive folder.')
        });
        return;
    }

    if (!frm.doc.custom_deal_name) {
        let clean_party = (frm.doc.customer_name || frm.doc.party_name || 'deal').toLowerCase().replace(/[^a-z0-9]/g, '');
        let service_code = typeof get_service_nomenclature === 'function' ? get_service_nomenclature(frm.doc.opportunity_type) : 'PS';
        let date_code = typeof get_deal_date_code === 'function' ? get_deal_date_code() : '261008';
        let generated = clean_party + '_' + service_code + '_' + date_code + '_1';
        frm.set_value('custom_deal_name', generated);
    }

    if (frm.is_new()) {
        frm.save().then(() => {
            create_and_open_drive_folder(frm);
        });
    } else {
        create_and_open_drive_folder(frm);
    }
}

function sync_opportunity_drive_link(frm) {
    if (frm.doc.opportunity_from === 'Lead' && frm.doc.party_name && !frm.doc.custom_google_drive_link) {
        frappe.db.get_doc('Lead', frm.doc.party_name).then(lead => {
            if (!lead) return;
            if (lead.custom_google_drive_link) {
                frm.set_value('custom_google_drive_link', lead.custom_google_drive_link);
            } else if (lead.notes && lead.notes.length > 0) {
                let note_text = lead.notes.map(n => n.note || '').join('\n');
                let m = note_text.match(/https:\/\/drive\.google\.com\/drive\/folders\/([a-zA-Z0-9_-]+)/i);
                if (m) {
                    frm.set_value('custom_google_drive_link', m[0]);
                }
            }
        });
    }
}

function rename_opportunity_drive_folder(frm, force) {
    if (!frm.doc.custom_google_drive_link || !frm.doc.custom_deal_name) return;
    if (!force && frm._last_renamed_deal === frm.doc.custom_deal_name) return;

    frappe.show_alert({
        message: __('Renaming Google Drive folder to ' + frm.doc.custom_deal_name + '...'),
        indicator: 'blue'
    });

    fetch('https://www.protovalue3d.in/api/gdrive/folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'rename',
            folder_url: frm.doc.custom_google_drive_link,
            new_name: frm.doc.custom_deal_name
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            frm._last_renamed_deal = frm.doc.custom_deal_name;
            frappe.show_alert({
                message: __('Google Drive folder renamed to ' + frm.doc.custom_deal_name),
                indicator: 'green'
            });
        } else {
            console.warn('[GDrive] Rename response:', data);
            if (force) {
                frappe.msgprint({
                    title: __('Rename Warning'),
                    indicator: 'orange',
                    message: data.message || 'Could not rename Drive folder'
                });
            }
        }
    })
    .catch(err => {
        console.error('[GDrive] Rename error:', err);
    });
}

function create_pdp_from_opportunity(frm) {
    function make_project(customer) {
        let doc = {
            doctype: 'Product Development Project',
            project_name: frm.doc.custom_deal_name || frm.doc.title || ('PDP - ' + customer),
            customer: customer,
            status: 'Prototyping & Testing',
            project_brief: frm.doc.description || '',
            start_date: frappe.datetime.get_today()
        };

        frappe.call({
            method: 'frappe.client.insert',
            args: { doc: doc },
            callback: function(r) {
                if (r.message) {
                    let proj_name = r.message.name;
                    frm.set_value('custom_product_development_project', proj_name);
                    frm.save().then(() => {
                        frappe.show_alert({
                            message: __('Product Development Project {0} created', [proj_name]),
                            indicator: 'green'
                        });
                        frappe.set_route('Form', 'Product Development Project', proj_name);
                    });
                }
            }
        });
    }

    if (frm.doc.opportunity_from === 'Customer') {
        make_project(frm.doc.party_name);
    } else {
        frappe.call({
            method: 'frappe.client.get_list',
            args: {
                doctype: 'Customer',
                filters: { lead_name: frm.doc.party_name },
                limit: 1
            },
            callback: function(r) {
                if (r.message && r.message.length > 0) {
                    make_project(r.message[0].name);
                } else {
                    frappe.prompt([
                        { fieldname: 'customer_name', fieldtype: 'Data', label: 'Customer Name', reqd: 1, default: frm.doc.customer_name || frm.doc.party_name }
                    ], function(val) {
                        frappe.call({
                            method: 'frappe.client.insert',
                            args: {
                                doc: {
                                    doctype: 'Customer',
                                    customer_name: val.customer_name,
                                    lead_name: frm.doc.party_name,
                                    customer_type: 'Individual',
                                    customer_group: 'All Customer Groups',
                                    territory: 'All Territories'
                                }
                            },
                            callback: function(res) {
                                if (res.message) {
                                    make_project(res.message.name);
                                }
                            }
                        });
                    }, __('Create Customer for Project'), __('Create'));
                }
            }
        });
    }
}

function create_and_open_drive_folder(frm) {
    if (frm.doc.custom_google_drive_link) {
        window.open(frm.doc.custom_google_drive_link, '_blank');
        return;
    }

    let deal = (frm.doc.custom_deal_name || frm.doc.name || 'Opportunity').trim();
    let newTab = window.open('about:blank', '_blank');

    frappe.show_alert({
        message: __('Creating Google Drive folder for <b>' + frappe.utils.escape_html(deal) + '</b>...'),
        indicator: 'blue'
    });

    fetch('https://www.protovalue3d.in/api/gdrive/folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'create',
            name: deal
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success && (data.folderUrl || data.folderId)) {
            let url = data.folderUrl || ('https://drive.google.com/drive/folders/' + data.folderId);
            frm.set_value('custom_google_drive_link', url);

            if (newTab) {
                newTab.location.href = url;
            } else {
                window.open(url, '_blank');
            }

            frm.save().then(() => {
                frappe.msgprint({
                    title: __('Google Drive Folder Ready'),
                    indicator: 'green',
                    message: __(
                        'Google Drive folder <b>' + frappe.utils.escape_html(deal) + '</b> created and opened in a new tab.<br><br>' +
                        '<b>Next Steps:</b><br>' +
                        '1. Upload your CAD or 3D files (.stl, .3mf, .step, etc.) into the Google Drive folder.<br>' +
                        '2. Return here and click <b>Fetch Files from Drive</b> on the File Details table to auto-populate.<br>' +
                        '3. Select the Material and Quantity for each file.'
                    )
                });
            });
        } else {
            if (newTab) newTab.close();
            frappe.msgprint({
                title: __('Folder Creation Failed'),
                indicator: 'red',
                message: data.message || 'Could not auto-create Google Drive folder.'
            });
        }
    })
    .catch(err => {
        if (newTab) newTab.close();
        frappe.msgprint({
            title: __('Error'),
            indicator: 'red',
            message: __('Failed to connect to Google Drive service: ' + err.message)
        });
    });
}

function fetch_drive_files_to_child_table(frm) {
    if (!frm.doc.custom_google_drive_link) {
        frappe.confirm(
            __('No Google Drive folder linked to this Opportunity yet. Would you like to create one now?'),
            () => { handle_create_drive_folder_click(frm); }
        );
        return;
    }

    frappe.show_alert({
        message: __('Fetching files from Google Drive...'),
        indicator: 'blue'
    });

    fetch('https://www.protovalue3d.in/api/gdrive/folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'list',
            folder_url: frm.doc.custom_google_drive_link
        })
    })
    .then(r => r.json())
    .then(data => {
        if (!data.success) {
            frappe.msgprint({
                title: __('Google Drive Sync Error'),
                indicator: 'red',
                message: data.message || 'Could not list files in the Drive folder.'
            });
            return;
        }

        let files = (data.files || []).filter(f => {
            let name = (f.name || '').trim();
            if (!name || name.startsWith('.') || f.mimeType === 'application/vnd.google-apps.folder') {
                return false;
            }
            return true;
        });

        if (files.length === 0) {
            frappe.msgprint({
                title: __('No Files in Drive Folder'),
                indicator: 'orange',
                message: __(
                    'No files found in the Google Drive folder yet.<br><br>' +
                    'Please upload your CAD or 3D files (.stl, .3mf, .step, .obj, etc.) into the folder first.<br><br>' +
                    '<a href="' + frm.doc.custom_google_drive_link + '" target="_blank" class="btn btn-primary btn-sm">' +
                    'Open Google Drive Folder</a>'
                )
            });
            return;
        }

        let existing_names = new Set((frm.doc.custom_file_details || []).map(r => (r.file_name || '').trim().toLowerCase()));
        let added_count = 0;

        files.forEach(f => {
            let fname = f.name.trim();
            if (existing_names.has(fname.toLowerCase())) {
                return;
            }

            let row = frm.add_child('custom_file_details');
            row.file_name = fname;
            row.quantity = 1;
            row.weight = 0;
            row.support_weight = 0;
            row.cost_per_gram = 0;
            row.total = 0;
            existing_names.add(fname.toLowerCase());
            added_count++;
        });

        frm.toggle_display('custom_file_details', true);
        frm.refresh_field('custom_file_details');
        frm.dirty();

        if (added_count > 0) {
            frappe.show_alert({
                message: __('Added ' + added_count + ' file(s) from Google Drive. Please fill Material and Quantity.'),
                indicator: 'green'
            });
        } else {
            frappe.show_alert({
                message: __('All ' + files.length + ' file(s) from Drive are already in the table.'),
                indicator: 'blue'
            });
        }
    })
    .catch(err => {
        frappe.msgprint({
            title: __('Fetch Error'),
            indicator: 'red',
            message: __('Error communicating with Google Drive service: ' + err.message)
        });
    });
}