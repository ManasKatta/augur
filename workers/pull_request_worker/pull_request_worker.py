#SPDX-License-Identifier: MIT
import ast
import json
import logging
import os
import sys
import time
import traceback
from workers.worker_git_integration import WorkerGitInterfaceable
from numpy.lib.utils import source
import requests
import copy
from datetime import datetime
from multiprocessing import Process, Queue
import pandas as pd
import sqlalchemy as s
from sqlalchemy.sql.expression import bindparam
from workers.worker_base import Worker

class GitHubPullRequestWorker(WorkerGitInterfaceable):
    """
    Worker that collects Pull Request related data from the
    Github API and stores it in our database.

    :param task: most recent task the broker added to the worker's queue
    :param config: holds info like api keys, descriptions, and database connection strings
    """
    def __init__(self, config={}):

        worker_type = "pull_request_worker"

        # Define what this worker can be given and know how to interpret
        given = [['github_url']]
        models = ['pull_requests', 'pull_request_commits', 'pull_request_files']

        # Define the tables needed to insert, update, or delete on
        data_tables = ['contributors', 'pull_requests',
            'pull_request_assignees', 'pull_request_events', 'pull_request_labels',
            'pull_request_message_ref', 'pull_request_meta', 'pull_request_repo',
            'pull_request_reviewers', 'pull_request_teams', 'message', 'pull_request_commits',
            'pull_request_files', 'pull_request_reviews', 'pull_request_review_message_ref']
        operations_tables = ['worker_history', 'worker_job']

        self.deep_collection = True
        self.platform_id = 25150 # GitHub

        # Run the general worker initialization
        super().__init__(worker_type, config, given, models, data_tables, operations_tables)

        # Define data collection info
        self.tool_source = 'GitHub Pull Request Worker'
        self.tool_version = '1.0.0'
        self.data_source = 'GitHub API'

        #Needs to be an attribute of the class for incremental database insert using paginate_endpoint
        self.pk_source_prs = []

    def graphql_paginate(self, query, data_subjects, before_parameters=None):
        """ Paginate a GitHub GraphQL query backwards

        :param query: A string, holds the GraphQL query
        :rtype: A Pandas DataFrame, contains all data contained in the pages
        """

        self.logger.info(f'Start paginate with params: \n{data_subjects} '
            f'\n{before_parameters}')

        def all_items(dictionary):
            for key, value in dictionary.items():
                if type(value) is dict:
                    yield (key, value)
                    yield from all_items(value)
                else:
                    yield (key, value)

        if not before_parameters:
            before_parameters = {}
            for subject, _ in all_items(data_subjects):
                before_parameters[subject] = ''

        start_cursor = None
        has_previous_page = True
        base_url = 'https://api.github.com/graphql'
        tuples = []

        def find_root_of_subject(data, key_subject):
            # self.logger.info(f'Finding {key_subject} root of {data}')
            key_nest = None
            for subject, nest in data.items():
                if key_subject in nest:
                    key_nest = nest[key_subject]
                    break
                elif type(nest) == dict:
                    return find_root_of_subject(nest, key_subject)
            else:
                raise KeyError
            return key_nest

        for data_subject, nest in data_subjects.items():

            # self.logger.info(f'Beginning paginate process for field {data_subject} '
            #     f'for query: {query}')

            page_count = 0
            while has_previous_page:

                page_count += 1

                num_attempts = 3
                success = False

                for attempt in range(num_attempts):
                    self.logger.info(f'Attempt #{attempt + 1} for hitting GraphQL endpoint '
                        f'page number {page_count}\n')

                    response = requests.post(base_url, json={'query': query.format(
                        **before_parameters)}, headers=self.headers)

                    self.update_gh_rate_limit(response)

                    try:
                        data = response.json()
                    except:
                        data = json.loads(json.dumps(response.text))

                    if 'errors' in data:
                        self.logger.info("Error!: {}".format(data['errors']))
                        if data['errors'][0]['type'] == 'NOT_FOUND':
                            self.logger.warning(
                                "Github repo was not found or does not exist for "
                                f"endpoint: {base_url}\n"
                            )
                            break
                        if data['errors'][0]['type'] == 'RATE_LIMITED':
                            self.update_gh_rate_limit(response)
                            num_attempts -= 1
                        continue


                    if 'data' in data:
                        success = True
                        root = find_root_of_subject(data, data_subject)
                        page_info = root['pageInfo']
                        data = root['edges']
                        break
                    else:
                        self.logger.info("Request returned a non-data dict: {}\n".format(data))
                        if data['message'] == 'Not Found':
                            self.logger.info(
                                "Github repo was not found or does not exist for endpoint: "
                                f"{base_url}\n"
                            )
                            break
                        if data['message'] == (
                            "You have triggered an abuse detection mechanism. Please wait a "
                            "few minutes before you try again."
                        ):
                            num_attempts -= 1
                            self.update_gh_rate_limit(response, temporarily_disable=True)
                        if data['message'] == "Bad credentials":
                            self.update_gh_rate_limit(response, bad_credentials=True)

                if not success:
                    self.logger.info('GraphQL query failed: {}'.format(query))
                    break

                before_parameters.update({
                    data_subject: ', before: \"{}\"'.format(page_info['startCursor'])
                })
                has_previous_page = page_info['hasPreviousPage']

                tuples += data

            self.logger.info(f"Paged through {page_count} pages and "
                f"collected {len(tuples)} data points\n")

            if not nest:
                return tuples

            return tuples + self.graphql_paginate(query, data_subjects[subject],
                before_parameters=before_parameters)


    def pull_request_files_model(self, task_info, repo_id):

        # query existing PRs and the respective url we will append the commits url to
        pr_number_sql = s.sql.text("""
            SELECT DISTINCT pr_src_number as pr_src_number, pull_requests.pull_request_id
            FROM pull_requests--, pull_request_meta
            WHERE repo_id = {}
        """.format(self.repo_id))
        pr_numbers = pd.read_sql(pr_number_sql, self.db, params={})

        pr_file_rows = []

        for index, pull_request in enumerate(pr_numbers.itertuples()):

            self.logger.info(f'Querying files for pull request #{index + 1} of {len(pr_numbers)}')

            query = """
                {{
                  repository(owner:"%s", name:"%s"){{
                    pullRequest (number: %s) {{
                """ % (self.owner, self.repo, pull_request.pr_src_number) + """
                      files (last: 100{files}) {{
                        pageInfo {{
                          hasPreviousPage
                          hasNextPage
                          endCursor
                          startCursor
                        }}
                        edges {{
                          node {{
                            additions
                            deletions
                            path
                          }}
                        }}
                      }}
                    }}
                  }}
                }}
            """

            pr_file_rows += [{
                'pull_request_id': pull_request.pull_request_id,
                'pr_file_additions': pr_file['node']['additions'],
                'pr_file_deletions': pr_file['node']['deletions'],
                'pr_file_path': pr_file['node']['path'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': 'GitHub API',
            } for pr_file in self.graphql_paginate(query, {'files': None})]


        # Get current table values
        table_values_sql = s.sql.text("""
            SELECT pull_request_files.*
            FROM pull_request_files, pull_requests
            WHERE pull_request_files.pull_request_id = pull_requests.pull_request_id
            AND repo_id = :repo_id
        """)
        # self.logger.info(
        #     f'Getting table values with the following PSQL query: \n{table_values_sql}\n'
        # )
        table_values = pd.read_sql(table_values_sql, self.db, params={'repo_id': self.repo_id})

        # Compare queried values against table values for dupes/updates
        if len(pr_file_rows) > 0:
            table_columns = pr_file_rows[0].keys()
        else:
            self.logger.info(f'No rows need insertion for repo {self.repo_id}\n')
            self.register_task_completion(task_info, self.repo_id, 'pull_request_files')
            return

        # Compare queried values against table values for dupes/updates
        pr_file_rows_df = pd.DataFrame(pr_file_rows)
        pr_file_rows_df = pr_file_rows_df.dropna(subset=['pull_request_id'])

        dupe_columns = ['pull_request_id', 'pr_file_path']
        update_columns = ['pr_file_additions', 'pr_file_deletions']

        need_insertion = pr_file_rows_df.merge(table_values, suffixes=('','_table'),
                            how='outer', indicator=True, on=dupe_columns).loc[
                                lambda x : x['_merge']=='left_only'][table_columns]

        need_updates = pr_file_rows_df.merge(table_values, on=dupe_columns, suffixes=('','_table'),
                        how='inner',indicator=False)[table_columns].merge(table_values,
                            on=update_columns, suffixes=('','_table'), how='outer',indicator=True
                                ).loc[lambda x : x['_merge']=='left_only'][table_columns]

        need_updates['b_pull_request_id'] = need_updates['pull_request_id']
        need_updates['b_pr_file_path'] = need_updates['pr_file_path']

        pr_file_insert_rows = need_insertion.to_dict('records')
        pr_file_update_rows = need_updates.to_dict('records')

        self.logger.info(
            f'Repo id {self.repo_id} needs {len(need_insertion)} insertions and '
            f'{len(need_updates)} updates.\n'
        )

        if len(pr_file_update_rows) > 0:
            success = False
            while not success:
                try:
                    self.db.execute(
                        self.pull_request_files_table.update().where(
                            self.pull_request_files_table.c.pull_request_id == bindparam(
                                'b_pull_request_id'
                            ) and self.pull_request_files_table.c.pr_file_path == bindparam(
                                'b_pr_file_path'
                            )
                        ).values(
                            pr_file_additions=bindparam('pr_file_additions'),
                            pr_file_deletions=bindparam('pr_file_deletions')
                        ), pr_file_update_rows
                    )
                    success = True
                except Exception as e:
                    self.logger.info('error: {}'.format(e))
                time.sleep(5)

        if len(pr_file_insert_rows) > 0:
            success = False
            while not success:
                try:
                    self.db.execute(
                        self.pull_request_files_table.insert(),
                        pr_file_insert_rows
                    )
                    success = True
                except Exception as e:
                    self.logger.info('error: {}'.format(e))
                time.sleep(5)

        self.register_task_completion(task_info, self.repo_id, 'pull_request_files')

    def pull_request_commits_model(self, task_info, repo_id):
        """ Queries the commits related to each pull request already inserted in the db """

        self.logger.info("Querying starting ids info...\n")

        # Increment so we are ready to insert the 'next one' of each of these most recent ids
        self.history_id = self.get_max_id(
            'worker_history', 'history_id', operations_table=True
        ) + 1
        self.pr_id_inc = self.get_max_id('pull_requests', 'pull_request_id')
        self.pr_meta_id_inc = self.get_max_id('pull_request_meta', 'pr_repo_meta_id')


        # query existing PRs and the respective url we will append the commits url to
        pr_url_sql = s.sql.text("""
            SELECT DISTINCT pr_url, pull_requests.pull_request_id
            FROM pull_requests--, pull_request_meta
            WHERE repo_id = {}
        """.format(self.repo_id))
        urls = pd.read_sql(pr_url_sql, self.db, params={})

        for pull_request in urls.itertuples(): # for each url of PRs we have inserted
            commits_url = pull_request.pr_url + '/commits?page={}'
            table = 'pull_request_commits'
            table_pkey = 'pr_cmt_id'
            duplicate_col_map = {'pr_cmt_sha': 'sha'}
            update_col_map = {}

            # Use helper paginate function to iterate the commits url and check for dupes
            #TODO: figure out why dupes sometimes still happen.q
            pr_commits = self.paginate(
                commits_url, duplicate_col_map, update_col_map, table, table_pkey,
                where_clause="where pull_request_id = {}".format(pull_request.pull_request_id)
            )

            for pr_commit in pr_commits: # post-pagination, iterate results
                if pr_commit['flag'] == 'need_insertion': # if non-dupe
                    pr_commit_row = {
                        'pull_request_id': pull_request.pull_request_id,
                        'pr_cmt_sha': pr_commit['sha'],
                        'pr_cmt_node_id': pr_commit['node_id'],
                        'pr_cmt_message': pr_commit['commit']['message'],
                        # 'pr_cmt_comments_url': pr_commit['comments_url'],
                        'tool_source': self.tool_source,
                        'tool_version': self.tool_version,
                        'data_source': 'GitHub API',
                    }
                    result = self.db.execute(
                        self.pull_request_commits_table.insert().values(pr_commit_row)
                    )
                    self.logger.info(
                        f"Inserted Pull Request Commit: {result.inserted_primary_key}\n"
                    )

        self.register_task_completion(self.task_info, self.repo_id, 'pull_request_commits')

    def _get_pk_source_prs(self):

        #self.owner and self.repo are both defined in the worker base's collect method using the url of the github repo.
        pr_url = (
            f"https://api.github.com/repos/{self.owner}/{self.repo}/pulls?state=all&"
            "direction=asc&per_page=100&page={}"
        )

        #Database action map is essential in order to avoid duplicates messing up the data
        pr_action_map = {
            'insert': {
                'source': ['id'],
                'augur': ['pr_src_id']
            },
            'update': {
                'source': ['state'],
                'augur': ['pr_src_state']
            }
        }

        #Use a parent method in order to iterate through pull request pages
        #Define a method to pass paginate_endpoint so that prs can be inserted incrementally

        def pk_source_increment_insert(inc_source_prs, action_map):

            self.write_debug_data(inc_source_prs, 'source_prs')

            if len(inc_source_prs['all']) == 0:
                self.logger.info("There are no prs for this repository.\n")
                self.register_task_completion(self.task_info, self.repo_id, 'pull_requests')
                return


            #self.logger.info(f"inc_source_prs is: {inc_source_prs} and the action map is {action_map}...")

            #This is sending empty data to enrich_cntrb_id, fix with check
            if len(inc_source_prs['insert']) > 0:
                inc_source_prs['insert'] = self.enrich_cntrb_id(
                    inc_source_prs['insert'], 'user.login', action_map_additions={
                        'insert': {
                            'source': ['user.node_id'],
                            'augur': ['gh_node_id']
                        }
                    }, prefix='user.'
                )
            else:
                self.logger.info("Contributor enrichment is not needed, no inserts in action map.")



            prs_insert = [
            {
                'repo_id': self.repo_id,
                'pr_url': pr['url'],
                'pr_src_id': pr['id'],
                'pr_src_node_id': None,
                'pr_html_url': pr['html_url'],
                'pr_diff_url': pr['diff_url'],
                'pr_patch_url': pr['patch_url'],
                'pr_issue_url': pr['issue_url'],
                'pr_augur_issue_id': None,
                'pr_src_number': pr['number'],
                'pr_src_state': pr['state'],
                'pr_src_locked': pr['locked'],
                'pr_src_title': pr['title'],
                'pr_augur_contributor_id': pr['cntrb_id'],
                'pr_body': pr['body'].encode(encoding='UTF-8',errors='backslashreplace').decode(encoding='UTF-8',errors='ignore') if (
                    pr['body']
                ) else None,
                'pr_created_at': pr['created_at'],
                'pr_updated_at': pr['updated_at'],
                'pr_closed_at': pr['closed_at'],
                'pr_merged_at': pr['merged_at'],
                'pr_merge_commit_sha': pr['merge_commit_sha'],
                'pr_teams': None,
                'pr_milestone': None if not (
                    pr['milestone'] and 'title' in pr['milestone']
                ) else pr['milestone']['title'],
                'pr_commits_url': pr['commits_url'],
                'pr_review_comments_url': pr['review_comments_url'],
                'pr_review_comment_url': pr['review_comment_url'],
                'pr_comments_url': pr['comments_url'],
                'pr_statuses_url': pr['statuses_url'],
                'pr_meta_head_id': None,
                'pr_meta_base_id': None,
                'pr_src_issue_url': pr['issue_url'],
                'pr_src_comments_url': pr['comments_url'], # NOTE: this seems redundant
                'pr_src_review_comments_url': pr['review_comments_url'], # this too
                'pr_src_commits_url': pr['commits_url'], # this one also seems redundant
                'pr_src_statuses_url': pr['statuses_url'],
                'pr_src_author_association': pr['author_association'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': 'GitHub API'
            } for pr in inc_source_prs['insert']
            ]

            #The b_pr_src_id bug comes from here
            if len(inc_source_prs['insert']) > 0 or len(inc_source_prs['update']) > 0:
                self.bulk_insert(
                    self.pull_requests_table,
                    update=inc_source_prs['update'], unique_columns=action_map['insert']['augur'],
                    insert=prs_insert, update_columns=action_map['update']['augur']
                )

                source_data = inc_source_prs['insert'] + inc_source_prs['update']

            elif not self.deep_collection:
                self.logger.info(
                    "There are no prs to update, insert, or collect nested information for.\n"
                )
                self.register_task_completion(self.task_info, self.repo_id, 'pull_requests')
                return

            if self.deep_collection:
                source_data = inc_source_prs['all']

            # Merge source data to inserted data to have access to inserted primary keys

            gh_merge_fields = ['id']
            augur_merge_fields = ['pr_src_id']

            self.pk_source_prs += self.enrich_data_primary_keys(source_data, self.pull_requests_table,
                gh_merge_fields, augur_merge_fields)
            return


        #paginate endpoint with stagger enabled so that the above method can insert every 500

        # self.logger.info(
        #     f"PR Action map is {pr_action_map}"
        # )

        source_prs = self.paginate_endpoint(
            pr_url, action_map=pr_action_map, table=self.pull_requests_table,
            where_clause=self.pull_requests_table.c.repo_id == self.repo_id,
            stagger=True,
            insertion_method=pk_source_increment_insert
        )

        # self.logger.info(
        #     f"PR Action map is {pr_action_map} after source_prs. The source_prs are {source_prs}."
        # )

        #Use the increment insert method in order to do the
        #remaining pages of the paginated endpoint that weren't inserted inside the paginate_endpoint method
        pk_source_increment_insert(source_prs,pr_action_map)

        pk_source_prs = self.pk_source_prs

        #This attribute is only needed because paginate endpoint needs to
        #send this data to the child class and this is the easiset way to do that.
        self.pk_source_prs = []

        return pk_source_prs

    def pull_requests_model(self, entry_info, repo_id):
        """Pull Request data collection function. Query GitHub API for PhubRs.

        :param entry_info: A dictionary consisiting of 'git_url' and 'repo_id'
        :type entry_info: dict
        """

        github_url = self.task_info['given']['github_url']

        # self.query_github_contributors(self.task_info, self.repo_id)

        self.logger.info("Beginning collection of Pull Requests...\n")
        self.logger.info(f"Repo ID: {self.repo_id}, Git URL: {github_url}\n")

        try: 
            pk_source_prs = self._get_pk_source_prs()
        except Exception as e: 
            self.logger(f"Pull Requests model failed with {e}.")

        self.write_debug_data(pk_source_prs, 'pk_source_prs')

        if pk_source_prs:
            try: 
                self.pull_request_comments_model()
            except Exception as e: 
                self.logger(f"Comments model failed with {e}.")
            try: 
                self.pull_request_events_model(pk_source_prs)
            except Exception as e: 
                self.logger(f"PR Events model failed with {e}.")
            try:
                self.pull_request_reviews_model(pk_source_prs)
            except Exception as e: 
                self.logger(f"PR Reviews model failed with {e}.")
            try: 
                self.pull_request_nested_data_model(pk_source_prs)
            except Exception as e: 
                self.logger(f"PR Nested Data model failed with {e}.")

        self.register_task_completion(self.task_info, self.repo_id, 'pull_requests')

    def pull_request_comments_model(self):

        comments_url = (
            f"https://api.github.com/repos/{self.owner}/{self.repo}/issues/comments?per_page=100"
            "&page={}"
        )

        # We should be capturing the following additional data here:
        # 1. The Platform message ID : Most efficient way to dup check
        # 2. The plaform issue ID and/or PR ID so queries are easier
        # 3. The REPO_ID so queries are easier.
        ## ALL THIS INFO IS IN THE PLATFOMR JSON AND WE ARe ignoring IT.

        comment_action_map = {
            'insert': {
                'source': ['created_at', 'body'],
                'augur': ['msg_timestamp', 'msg_text']
            }
        }

        # TODO: add relational table so we can include a where_clause here
        pr_comments = self.new_paginate_endpoint(
            comments_url, action_map=comment_action_map, table=self.message_table
        )

        self.write_debug_data(pr_comments, 'pr_comments')

        pr_comments['insert'] = self.text_clean(pr_comments['insert'], 'body')
        #This is sending empty data to enrich_cntrb_id, fix with check
        if len(pr_comments['insert']) > 0:
            pr_comments['insert'] = self.enrich_cntrb_id(
                pr_comments['insert'], 'user.login', action_map_additions={
                    'insert': {
                        'source': ['user.node_id'],
                        'augur': ['gh_node_id']
                    }
                }, prefix='user.'
            )
        else:
            self.logger.info("Contributor enrichment is not needed, no inserts in action map.")

        pr_comments_insert = [
            {
                'pltfrm_id': self.platform_id,
                'msg_text': comment['body'].encode(encoding='UTF-8',errors='backslashreplace').decode(encoding='UTF-8',errors='ignore') if (
                    comment['body']
                ) else None,
                'msg_timestamp': comment['created_at'],
                'cntrb_id': comment['cntrb_id'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source,
                'pr_message_ref_src_comment_id': comment['id'],
                'pr_message_ref_src_node_id': comment['node_id']
            } for comment in pr_comments['insert']
        ]

        self.bulk_insert(self.message_table, insert=pr_comments_insert)

        # PR MESSAGE REF TABLE

        c_pk_source_comments = self.enrich_data_primary_keys(pr_comments['insert'],
            self.message_table, ['created_at', 'body'], ['msg_timestamp', 'msg_text'])

        self.write_debug_data(c_pk_source_comments, 'c_pk_source_comments')

        both_pk_source_comments = self.enrich_data_primary_keys(c_pk_source_comments,
            self.pull_requests_table, ['issue_url'], ['pr_issue_url'])

        self.write_debug_data(both_pk_source_comments, 'both_pk_source_comments')

        pr_message_ref_insert = [
            {
                'pull_request_id': comment['pull_request_id'],
                'msg_id': comment['msg_id'],
                'pr_message_ref_src_comment_id': comment['id'],
                'pr_message_ref_src_node_id': comment['node_id'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source
            } for comment in both_pk_source_comments
        ]

        self.bulk_insert(self.pull_request_message_ref_table, insert=pr_message_ref_insert)

    def pull_request_events_model(self, pk_source_prs=[]):

        if not pk_source_prs:
            pk_source_prs = self._get_pk_source_prs()

        events_url = (
            f"https://api.github.com/repos/{self.owner}/{self.repo}/issues/events?per_page=100&"
            "page={}"
        )

        # Get events that we already have stored
        #   Set pseudo key (something other than PK) to
        #   check dupicates with
        event_action_map = {
            'insert': {
                'source': ['url'],
                'augur': ['node_url']
            }
        }

        #list to hold contributors needing insertion or update
        pr_events = self.new_paginate_endpoint(
            events_url, table=self.pull_request_events_table, action_map=event_action_map,
            where_clause=self.pull_request_events_table.c.pull_request_id.in_(
                set(pd.DataFrame(pk_source_prs)['pull_request_id'])
            )
        )

        self.write_debug_data(pr_events, 'pr_events')

        pk_pr_events = self.enrich_data_primary_keys(pr_events['insert'],
            self.pull_requests_table, ['issue.url'], ['pr_issue_url'])

        self.write_debug_data(pk_pr_events, 'pk_pr_events')

        if len(pk_pr_events) > 0:
            pk_pr_events = self.enrich_cntrb_id(
                pk_pr_events, 'actor.login', action_map_additions={
                    'insert': {
                        'source': ['actor.node_id'],
                        'augur': ['gh_node_id']
                    }
                }, prefix='actor.'
            )
        else:
            self.logger.info("Contributor enrichment is not needed, no data provided.")

        pr_events_insert = [
            {
                'pull_request_id': event['pull_request_id'],
                'cntrb_id': event['cntrb_id'],
                'action': event['event'],
                'action_commit_hash': None,
                'created_at': event['created_at'],
                'issue_event_src_id': event['id'],
                'node_id': event['node_id'],
                'node_url': event['url'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source
            } for event in pk_pr_events if event['actor'] is not None
        ]

        self.bulk_insert(self.pull_request_events_table, insert=pr_events_insert)

    def pull_request_reviews_model(self, pk_source_prs=[]):

        if not pk_source_prs:
            pk_source_prs = self._get_pk_source_prs()

        review_action_map = {
            'insert': {
                'source': ['id'],
                'augur': ['pr_review_src_id']
            },
            'update': {
                'source': ['state'],
                'augur': ['pr_review_state']
            }
        }

        reviews_urls = [
            (
                f"https://api.github.com/repos/{self.owner}/{self.repo}/pulls/{pr['number']}/"
                "reviews?per_page=100", {'pull_request_id': pr['pull_request_id']}
            )
            for pr in pk_source_prs
        ]

        pr_pk_source_reviews = self.multi_thread_urls(reviews_urls)
        self.write_debug_data(pr_pk_source_reviews, 'pr_pk_source_reviews')

        cols_to_query = self.get_relevant_columns(
            self.pull_request_reviews_table, review_action_map
        )

        #I don't know what else this could be used for so I'm using it for the function call
        table_values = self.db.execute(s.sql.select(cols_to_query).where(
            self.pull_request_reviews_table.c.pull_request_id.in_(
                    set(pd.DataFrame(pk_source_prs)['pull_request_id'])
                ))).fetchall()

        source_reviews_insert, source_reviews_update = self.organize_needed_data(
            pr_pk_source_reviews, table_values=table_values,
            action_map=review_action_map
        )

        if len(source_reviews_insert) > 0:
            source_reviews_insert = self.enrich_cntrb_id(
                source_reviews_insert, 'user.login', action_map_additions={
                    'insert': {
                        'source': ['user.node_id'],
                        'augur': ['gh_node_id']
                    }
                }, prefix='user.'
            )
        else:
            self.logger.info("Contributor enrichment is not needed, source_reviews_insert is empty.")

        reviews_insert = [
            {
                'pull_request_id': review['pull_request_id'],
                'cntrb_id': review['cntrb_id'],
                'pr_review_author_association': review['author_association'],
                'pr_review_state': review['state'],
                'pr_review_body': review['body'].encode(encoding='UTF-8',errors='backslashreplace').decode(encoding='UTF-8',errors='ignore') if (
                    review['body']
                ) else None,
                'pr_review_submitted_at': review['submitted_at'] if (
                    'submitted_at' in review
                ) else None,
                'pr_review_src_id': review['id'],
                'pr_review_node_id': review['node_id'],
                'pr_review_html_url': review['html_url'],
                'pr_review_pull_request_url': review['pull_request_url'],
                'pr_review_commit_id': review['commit_id'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source,
                'repo_id': self.repo_id
            } for review in source_reviews_insert if review['user'] and 'login' in review['user']
        ]

        self.bulk_insert(
            self.pull_request_reviews_table, insert=reviews_insert, update=source_reviews_update,
            unique_columns=review_action_map['insert']['augur'],
            update_columns=review_action_map['update']['augur']
        )

        # Merge source data to inserted data to have access to inserted primary keys

        gh_merge_fields = ['id']
        augur_merge_fields = ['pr_review_src_id']

        both_pr_review_pk_source_reviews = self.enrich_data_primary_keys(
            pr_pk_source_reviews, self.pull_request_reviews_table, gh_merge_fields,
            augur_merge_fields
        )
        self.write_debug_data(both_pr_review_pk_source_reviews, 'both_pr_review_pk_source_reviews')

        # Review Comments

       #  https://api.github.com/repos/chaoss/augur/pulls

        review_msg_url = (f'https://api.github.com/repos/{self.owner}/{self.repo}/pulls' +
            '/comments?per_page=100&page={}')

        review_msg_action_map = {
            'insert': {
                'source': ['created_at', 'body'],
                'augur': ['msg_timestamp', 'msg_text']
            }
        }

        in_clause = [] if len(both_pr_review_pk_source_reviews) == 0 else \
            set(pd.DataFrame(both_pr_review_pk_source_reviews)['pr_review_id'])

        review_msgs = self.paginate_endpoint(
            review_msg_url, action_map=review_msg_action_map, table=self.message_table,
            where_clause=self.message_table.c.msg_id.in_(
                [
                    msg_row[0] for msg_row in self.db.execute(
                        s.sql.select([self.pull_request_review_message_ref_table.c.msg_id]).where(
                            self.pull_request_review_message_ref_table.c.pr_review_id.in_(
                                in_clause
                            )
                        )
                    ).fetchall()
                ]
            )
        )
        self.write_debug_data(review_msgs, 'review_msgs')

        if len(review_msgs['insert']) > 0:
            review_msgs['insert'] = self.enrich_cntrb_id(
                review_msgs['insert'], 'user.login', action_map_additions={
                    'insert': {
                        'source': ['user.node_id'],
                        'augur': ['gh_node_id']
                    }
                }, prefix='user.'
            )
        else:
            self.logger.info("Contributor enrichment is not needed, nothing to insert from the action map.")

        review_msg_insert = [
            {
                'pltfrm_id': self.platform_id,
                'msg_text': comment['body'].encode(encoding='UTF-8',errors='backslashreplace').decode(encoding='UTF-8',errors='ignore') if (
                    comment['body']
                ) else None,
                'msg_timestamp': comment['created_at'],
                'cntrb_id': comment['cntrb_id'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source
            } for comment in review_msgs['insert']
            if comment['user'] and 'login' in comment['user']
        ]

        self.bulk_insert(self.message_table, insert=review_msg_insert)

        # PR REVIEW MESSAGE REF TABLE

        c_pk_source_comments = self.enrich_data_primary_keys(
            review_msgs['insert'], self.message_table, ['created_at', 'body'],
            ['msg_timestamp', 'msg_text']
        )
        self.write_debug_data(c_pk_source_comments, 'c_pk_source_comments')

        both_pk_source_comments = self.enrich_data_primary_keys(
            c_pk_source_comments, self.pull_request_reviews_table, ['pull_request_review_id'],
            ['pr_review_src_id']
        )
        self.write_debug_data(both_pk_source_comments, 'both_pk_source_comments')

        pr_review_msg_ref_insert = [
            {
                'pr_review_id': comment['pr_review_id'],
                'msg_id': comment['msg_id'],
                'pr_review_msg_url': comment['url'],
                'pr_review_src_id': comment['pull_request_review_id'],
                'pr_review_msg_src_id': comment['id'],
                'pr_review_msg_node_id': comment['node_id'],
                'pr_review_msg_diff_hunk': comment['diff_hunk'],
                'pr_review_msg_path': comment['path'],
                'pr_review_msg_position': comment['position'],
                'pr_review_msg_original_position': comment['original_position'],
                'pr_review_msg_commit_id': comment['commit_id'],
                'pr_review_msg_original_commit_id': comment['original_commit_id'],
                'pr_review_msg_updated_at': comment['updated_at'],
                'pr_review_msg_html_url': comment['html_url'],
                'pr_url': comment['pull_request_url'],
                'pr_review_msg_author_association': comment['author_association'],
                'pr_review_msg_start_line': comment['start_line'],
                'pr_review_msg_original_start_line': comment['original_start_line'],
                'pr_review_msg_start_side': comment['start_side'],
                'pr_review_msg_line': comment['line'],
                'pr_review_msg_original_line': comment['original_line'],
                'pr_review_msg_side': comment['side'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source,
                'repo_id': self.repo_id 
            } for comment in both_pk_source_comments
        ]

        self.bulk_insert(
            self.pull_request_review_message_ref_table,
            insert=pr_review_msg_ref_insert
        )

    def pull_request_nested_data_model(self, pk_source_prs=[]):

        if not pk_source_prs:
            pk_source_prs = self._get_pk_source_prs()

        labels_all = []
        reviewers_all = []
        assignees_all = []
        meta_all = []

        for index, pr in enumerate(pk_source_prs):

            # PR Labels
            source_labels = pd.DataFrame(pr['labels'])
            source_labels['pull_request_id'] = pr['pull_request_id']
            labels_all += source_labels.to_dict(orient='records')

            # Reviewers
            source_reviewers = pd.DataFrame(pr['requested_reviewers'])
            source_reviewers['pull_request_id'] = pr['pull_request_id']
            reviewers_all += source_reviewers.to_dict(orient='records')

            # Assignees
            source_assignees = pd.DataFrame(pr['assignees'])
            source_assignees['pull_request_id'] = pr['pull_request_id']
            assignees_all += source_assignees.to_dict(orient='records')

            # Meta
            pr['head'].update(
                {'pr_head_or_base': 'head', 'pull_request_id': pr['pull_request_id']}
            )
            pr['base'].update(
                {'pr_head_or_base': 'base', 'pull_request_id': pr['pull_request_id']}
            )
            meta_all += [pr['head'], pr['base']]

        # PR labels insertion
        label_action_map = {
            'insert': {
                'source': ['pull_request_id', 'id'],
                'augur': ['pull_request_id', 'pr_src_id']
            }
        }


        table_values_pr_labels = self.db.execute(
            s.sql.select(self.get_relevant_columns(self.pull_request_labels_table,label_action_map))
        ).fetchall()

        source_labels_insert, _ = self.organize_needed_data(
            labels_all, table_values=table_values_pr_labels, action_map=label_action_map
        )
        labels_insert = [
            {
                'pull_request_id': label['pull_request_id'],
                'pr_src_id': label['id'],
                'pr_src_node_id': label['node_id'],
                'pr_src_url': label['url'],
                'pr_src_description': label['name'],
                'pr_src_color': label['color'],
                'pr_src_default_bool': label['default'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source
            } for label in source_labels_insert
        ]
        self.bulk_insert(self.pull_request_labels_table, insert=labels_insert)

        # PR reviewers insertion
        reviewer_action_map = {
            'insert': {
                'source': ['pull_request_id', 'id'],
                'augur': ['pull_request_id', 'pr_reviewer_src_id']
            }
        }

        table_values_issue_labels = self.db.execute(
            s.sql.select(self.get_relevant_columns(self.pull_request_reviewers_table,reviewer_action_map))
        ).fetchall()

        source_reviewers_insert, _ = self.organize_needed_data(
            reviewers_all, table_values=table_values_issue_labels,
            action_map=reviewer_action_map
        )

        if len(source_reviewers_insert) > 0:
            source_reviewers_insert = self.enrich_cntrb_id(
                source_reviewers_insert, 'login', action_map_additions={
                    'insert': {
                        'source': ['node_id'],
                        'augur': ['gh_node_id']
                    }
                }
            )
        else:
            self.logger.info("Contributor enrichment is not needed, no inserts provided.")

        reviewers_insert = [
            {
                'pull_request_id': reviewer['pull_request_id'],
                'cntrb_id': reviewer['cntrb_id'],
                'pr_reviewer_src_id': int(float(reviewer['id'])),
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source,
                'repo_id': self.repo_id 
            } for reviewer in source_reviewers_insert if 'login' in reviewer
        ]
        self.bulk_insert(self.pull_request_reviewers_table, insert=reviewers_insert)

        # PR assignees insertion
        assignee_action_map = {
            'insert': {
                'source': ['pull_request_id', 'id'],
                'augur': ['pull_request_id', 'pr_assignee_src_id']
            }
        }


        table_values_assignees_labels = self.db.execute(
            s.sql.select(self.get_relevant_columns(self.pull_request_assignees_table,assignee_action_map))
        ).fetchall()

        source_assignees_insert, _ = self.organize_needed_data(
            assignees_all, table_values=table_values_assignees_labels,
            action_map=assignee_action_map
        )

        if len(source_assignees_insert) > 0:
            source_assignees_insert = self.enrich_cntrb_id(
                source_assignees_insert, 'login', action_map_additions={
                    'insert': {
                        'source': ['node_id'],
                        'augur': ['gh_node_id']
                    }
                }
            )
        else:
            self.logger.info("Contributor enrichment is not needed, no inserts provided.")


        assignees_insert = [
            {
                'pull_request_id': assignee['pull_request_id'],
                'contrib_id': assignee['cntrb_id'],
                'pr_assignee_src_id': assignee['id'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source
            } for assignee in source_assignees_insert if 'login' in assignee
        ]
        self.bulk_insert(self.pull_request_assignees_table, insert=assignees_insert)

        # PR meta insertion
        meta_action_map = {
            'insert': {
                'source': ['pull_request_id', 'sha', 'pr_head_or_base'],
                'augur': ['pull_request_id', 'pr_sha', 'pr_head_or_base']
            }
        }

        table_values_pull_request_meta = self.db.execute(
            s.sql.select(self.get_relevant_columns(self.pull_request_meta_table,meta_action_map))
        ).fetchall()

        source_meta_insert, _ = self.organize_needed_data(
            meta_all, table_values=table_values_pull_request_meta, action_map=meta_action_map
        )


        if len(source_meta_insert) > 0:
            source_meta_insert = self.enrich_cntrb_id(
                source_meta_insert, 'user.login', action_map_additions={
                    'insert': {
                        'source': ['user.node_id'],
                        'augur': ['gh_node_id']
                    }
                }, prefix='user.'
            )
        else:
            self.logger.info("Contributor enrichment is not needed, nothing in source_meta_insert.")

        meta_insert = [
            {
                'pull_request_id': meta['pull_request_id'],
                'pr_head_or_base': meta['pr_head_or_base'],
                'pr_src_meta_label': meta['label'],
                'pr_src_meta_ref': meta['ref'],
                'pr_sha': meta['sha'],
                'cntrb_id': meta['cntrb_id'],
                'tool_source': self.tool_source,
                'tool_version': self.tool_version,
                'data_source': self.data_source
            } for meta in source_meta_insert if meta['user'] and 'login' in meta['user']
        ]
        self.bulk_insert(self.pull_request_meta_table, insert=meta_insert)

    def query_pr_repo(self, pr_repo, pr_repo_type, pr_meta_id):
        """ TODO: insert this data as extra columns in the meta table """
        self.logger.info(f'Querying PR {pr_repo_type} repo')

        table = 'pull_request_repo'
        duplicate_col_map = {'pr_src_repo_id': 'id'}
        update_col_map = {}
        table_pkey = 'pr_repo_id'

        update_keys = list(update_col_map.keys()) if update_col_map else []
        cols_query = list(duplicate_col_map.keys()) + update_keys + [table_pkey]

        pr_repo_table_values = self.get_table_values(cols_query, [table])

        new_pr_repo = self.assign_tuple_action(
            [pr_repo], pr_repo_table_values, update_col_map, duplicate_col_map, table_pkey
        )[0]

        if new_pr_repo['owner'] and 'login' in new_pr_repo['owner']:
            cntrb_id = self.find_id_from_login(new_pr_repo['owner']['login'])
        else:
            cntrb_id = 1

        pr_repo = {
            'pr_repo_meta_id': pr_meta_id,
            'pr_repo_head_or_base': pr_repo_type,
            'pr_src_repo_id': new_pr_repo['id'],
            # 'pr_src_node_id': new_pr_repo[0]['node_id'],
            'pr_src_node_id': None,
            'pr_repo_name': new_pr_repo['name'],
            'pr_repo_full_name': new_pr_repo['full_name'],
            'pr_repo_private_bool': new_pr_repo['private'],
            'pr_cntrb_id': cntrb_id,
            'tool_source': self.tool_source,
            'tool_version': self.tool_version,
            'data_source': self.data_source
        }

        if new_pr_repo['flag'] == 'need_insertion':
            result = self.db.execute(self.pull_request_repo_table.insert().values(pr_repo))
            self.logger.info(f"Added PR {pr_repo_type} repo {result.inserted_primary_key}")

            self.results_counter += 1

            self.logger.info(
                f"Finished adding PR {pr_repo_type} Repo data for PR with id {self.pr_id_inc}"
            )
