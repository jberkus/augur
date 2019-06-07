from multiprocessing import Process, Queue
from urllib.parse import urlparse
import requests
import pandas as pd
import sqlalchemy as s
from sqlalchemy.ext.automap import automap_base
from sqlalchemy import MetaData
import datetime


class CollectorTask:
    """ Worker's perception of a task in its queue
    Holds a message type (EXIT, TASK, etc) so the worker knows how to process the queue entry
    and the github_url given that it will be collecting data for
    """
    def __init__(self, message_type='TASK', entry_info=None):
        self.type = message_type
        self.entry_info = entry_info

def dump_queue(queue):
    """
    Empties all pending items in a queue and returns them in a list.
    """
    result = []
    queue.put("STOP")
    for i in iter(queue.get, 'STOP'):
        result.append(i)
    # time.sleep(.1)
    return result



class GitHubWorker:
    """ Worker that collects data from the Github API and stores it in our database
    task: most recent task the broker added to the worker's queue
    child: current process of the queue being ran
    queue: queue of tasks to be fulfilled
    config: holds info like api keys, descriptions, and database connection strings
    """
    def __init__(self, config, task=None):
        self._task = task
        self._child = None
        self._queue = Queue()
        self.config = config
        self.db = None
        self.table = None
        self.API_KEY = self.config['key']
        self.tool_source = 'GitHub API Worker'
        self.tool_version = '0.0.1' # See __init__.py
        self.data_source = 'GitHub API'

        url = "https://api.github.com/users/gabe-heim"
        response = requests.get(url=url)
        self.rate_limit = int(response.headers['X-RateLimit-Remaining'])

        
        specs = {
            "id": self.config['id'],
            "location": "http://localhost:51236",
            "qualifications":  [
                {
                    "given": [["git_url"]],
                    "models":["issues"]
                }
            ],
            "config": [self.config]
        }

        self.DB_STR = 'postgresql://{}:{}@{}:{}/{}'.format(
            self.config['user'], self.config['password'], self.config['host'], self.config['port'], self.config['database']
        )
        
        dbschema = 'augur_data'
        self.db = s.create_engine(self.DB_STR, poolclass = s.pool.NullPool,
            connect_args={'options': '-csearch_path={}'.format(dbschema)})

        metadata = MetaData()

        metadata.reflect(self.db, only=['contributors', 'issues', 'issue_labels', 'message',
            'issue_message_ref', 'issue_events',
            'issue_assignees'])

        Base = automap_base(metadata=metadata)

        Base.prepare()

        self.contributors_table = Base.classes.contributors.__table__
        self.issues_table = Base.classes.issues.__table__
        self.issue_labels_table = Base.classes.issue_labels.__table__
        self.issue_events_table = Base.classes.issue_events.__table__
        self.message_table = Base.classes.message.__table__
        self.issues_message_ref_table = Base.classes.issue_message_ref.__table__
        self.issue_assignees_table = Base.classes.issue_assignees.__table__


        # Query all repos // CHANGE THIS
        repoUrlSQL = s.sql.text("""
            SELECT repo_git, repo_id FROM repo
            """)

        rs = pd.read_sql(repoUrlSQL, self.db, params={})

        # Populate queue
        for index, row in rs.iterrows():
            self._queue.put(CollectorTask(message_type='TASK', entry_info=row))

        #
        maxIssueCntrbSQL = s.sql.text("""
            SELECT max(issues.issue_id) AS issue_id, max(contributors.cntrb_id) AS cntrb_id
            FROM issues, contributors
        """)
        rs = pd.read_sql(maxIssueCntrbSQL, self.db, params={})

        issue_start = rs.iloc[0]["issue_id"]
        cntrb_start = rs.iloc[0]["cntrb_id"]

        maxMsgSQL = s.sql.text("""
            SELECT max(msg_id) AS msg_id
            FROM message
        """)
        rs = pd.read_sql(maxMsgSQL, self.db, params={})

        msg_start = rs.iloc[0]["msg_id"]

        if issue_start is None:
            issue_start = 25150
        else:
            issue_start = issue_start.item()
        if cntrb_start is None:
            cntrb_start = 25150
        else:
            cntrb_start = cntrb_start.item()
        if msg_start is None:
            msg_start = 25150
        else:
            msg_start = msg_start.item()
        self.issue_id_inc = (issue_start + 1)
        self.cntrb_id_inc = (cntrb_start + 1)
        self.msg_id_inc = (msg_start + 1)

        self.run()

        requests.post('http://localhost:5000/api/workers', json=specs) #hello message

    def update_config(self, config):
        """ Method to update config and set a default
        """
        self.config = {
            'database_connection_string': 'psql://localhost:5433/augur',
            "display_name": "",
            "description": "",
            "required": 1,
            "type": "string"
        }
        self.config.update(config)
        self.API_KEY = self.config['key']

    @property
    def task(self):
        """ Property that is returned when the worker's current task is referenced
        """
        return self._task
    
    @task.setter
    def task(self, value):
        """ entry point for the broker to add a task to the queue
        Adds this task to the queue, and calls method to process queue
        """
        git_url = value['given']['git_url']

        """ Query all repos """
        repoUrlSQL = s.sql.text("""
            SELECT repo_id FROM repo WHERE repo_git = '{}'
            """.format(git_url))
        rs = pd.read_sql(repoUrlSQL, self.db, params={})

        try:
            self._queue.put(CollectorTask(message_type='TASK', entry_info={"git_url": git_url, "repo_id": rs}))
        
        # list_queue = dump_queue(self._queue)
        # print("worker's queue after adding the job: " + list_queue)

        except:
            print("that repo is not in our database")
        
        self._task = value
        self.run()

    def cancel(self):
        """ Delete/cancel current task
        """
        self._task = None

    def run(self):
        """ Kicks off the processing of the queue if it is not already being processed
        Gets run whenever a new task is added
        """
        print("Running...")
        # if not self._child:
        self._child = Process(target=self.collect, args=())
        self._child.start()

    def collect(self):
        """ Function to process each entry in the worker's task queue
        Determines what action to take based off the message type
        """
        while True:
            if not self._queue.empty():
                message = self._queue.get()
            else:
                break

            if message.type == 'EXIT':
                break

            if message.type != 'TASK':
                raise ValueError(f'{message.type} is not a recognized task type')

            if message.type == 'TASK':
                self.query_issues(message.entry_info)

    def query_contributors(self, entry_info):

        """ Data collection function
        Query the GitHub API for contributors
        """

        print("Querying contributors with given entry info: ", entry_info, "\n")

        # Url of repo we are querying for
        url = entry_info['repo_git']

        # Extract owner/repo from the url for the endpoint
        path = urlparse(url)
        split = path[2].split('/')

        owner = split[1]
        name = split[2]

        # Handles git url case by removing the extension
        if ".git" in name:
            name = name[:-4]

        url = ("https://api.github.com/repos/" + owner + "/" + name + "/contributors")
        print("Hitting endpoint: ", url, " ...\n")
        r = requests.get(url=url)
        self.update_rate_limit()
        contributors = r.json()

        # Duplicate checking ...
        need_insertion = self.filter_duplicates({'cntrb_login': "login"}, ['contributors'], contributors)
        print("Count of contributors needing insertion: ", len(need_insertion), "\n")
        
        for repo_contributor in need_insertion:

            # Need to hit this single contributor endpoint to get extra data including...
            #   created at
            #   i think that's it
            cntrb_url = ("https://api.github.com/users/" + repo_contributor['login'])
            print("Hitting endpoint: ", cntrb_url, " ...\n")
            r = requests.get(url=cntrb_url)
            self.update_rate_limit()
            contributor = r.json()


            # NEED TO FIGURE OUT IF THIS STUFF IS EVER AVAILABLE
            #    if so, the null case will need to be handled

            # "company": contributor['company'],
            # "location": contributor['location'],
            # "email": contributor['email'],

            # aliasSQL = s.sql.text("""
            #     SELECT canonical_email
            #     FROM contributors_aliases
            #     WHERE alias_email = {}
            # """.format(contributor['email']))
            # rs = pd.read_sql(aliasSQL, self.db, params={})

            canonical_email = None#rs.iloc[0]["canonical_email"]



            cntrb = {
                "cntrb_login": contributor['login'],
                "cntrb_created_at": contributor['created_at'],
                # "cntrb_type": , dont have a use for this as of now ... let it default to null
                "cntrb_canonical": canonical_email,
                "gh_user_id": contributor['id'],
                "gh_login": contributor['login'],
                "gh_url": contributor['url'],
                "gh_html_url": contributor['html_url'],
                "gh_node_id": contributor['node_id'],
                "gh_avatar_url": contributor['avatar_url'],
                "gh_gravatar_id": contributor['gravatar_id'],
                "gh_followers_url": contributor['followers_url'],
                "gh_following_url": contributor['following_url'],
                "gh_gists_url": contributor['gists_url'],
                "gh_starred_url": contributor['starred_url'],
                "gh_subscriptions_url": contributor['subscriptions_url'],
                "gh_organizations_url": contributor['organizations_url'],
                "gh_repos_url": contributor['repos_url'],
                "gh_events_url": contributor['events_url'],
                "gh_received_events_url": contributor['received_events_url'],
                "gh_type": contributor['type'],
                "gh_site_admin": contributor['site_admin'],
                "tool_source": self.tool_source,
                "tool_version": self.tool_version,
                "data_source": self.data_source
            }

            # Commit insertion to table
            self.db.execute(self.contributors_table.insert().values(cntrb))
            print("Inserted contributor: ", contributor['login'], "\n")

            # Increment our global track of the cntrb id for the possibility of it being used as a FK
            self.cntrb_id_inc += 1
            

    def query_issues(self, entry_info):

        """ Data collection function
        Query the GitHub API for issues
        """

        # Contributors are part of this model, and finding all for the repo saves us 
        #   from having to add them as we discover committers in the issue process
        self.query_contributors(entry_info)

        url = entry_info['repo_git']

        # Extract the owner/repo for the endpoint
        path = urlparse(url)
        split = path[2].split('/')

        owner = split[1]
        name = split[2]

        # Handle git url case by removing extension
        if ".git" in name:
            name = name[:-4]

        url = ("https://api.github.com/repos/" + owner + "/" + name + "/issues")
        print("Hitting endpoint: ", url, " ...\n")
        r = requests.get(url=url)
        self.update_rate_limit()
        issues = r.json()

        # To store GH's issue numbers that are used in other endpoints for events and comments
        issue_numbers = []

        # Discover and remove duplicates before we start inserting
        need_insertion = self.filter_duplicates({'gh_issue_id': 'id'}, ['issues'], issues)
        print("Count of issues needing insertion: ", len(need_insertion), "\n")

        for issue_dict in need_insertion:

            print("Begin analyzing the issue with title: ", issue_dict['title'], "\n")
            # Add the FK repo_id to the dict being inserted
            issue_dict['repo_id'] = entry_info['repo_id']

            # Figure out if this issue is a PR
            #   still unsure about this key value pair/what it means
            pr_id = None
            if "pull_request" in issue_dict:
                print("it is a PR\n")
                # Right now we are just storing our issue id as the PR id if it is one
                pr_id = self.issue_id_inc
            else:
                print("it is not a PR\n")

            # Begin on the actual issue...

            # Base of the url for comment and event endpoints
            url = ("https://api.github.com/repos/" + owner + "/" + name + "/issues/" + str(issue_dict['number']))

            # Get events ready in case the issue is closed and we need to insert the closer's id
            events_url = (url + "/events")
            print("Hitting endpoint: ", events_url, " ...\n")
            r = requests.get(url=events_url)
            self.update_rate_limit()
            issue_events = r.json()
            
            # If the issue is closed, then we search for the closing event and store the user's id
            cntrb_id = None
            if issue_dict['closed_at'] is not None:
                for event in issue_events:
                    if event['event'] == 'closed':
                        cntrb_id = self.find_id_from_login(event['actor']['login'])
            
            issue = {
                "issue_id": self.issue_id_inc,
                "repo_id": issue_dict['repo_id'],
                "reporter_id": self.find_id_from_login(issue_dict['user']['login']),
                "pull_request": pr_id,
                "pull_request_id": pr_id,
                "created_at": issue_dict['created_at'],
                "issue_title": issue_dict['title'],
                "issue_body": issue_dict['body'],
                "cntrb_id": cntrb_id,
                "comment_count": issue_dict['comments'],
                "updated_at": issue_dict['updated_at'],
                "closed_at": issue_dict['closed_at'],
                "repository_url": issue_dict['repository_url'],
                "issue_url": issue_dict['url'],
                "labels_url": issue_dict['labels_url'],
                "comments_url": issue_dict['comments_url'],
                "events_url": issue_dict['events_url'],
                "html_url": issue_dict['html_url'],
                "issue_state": issue_dict['state'],
                "issue_node_id": issue_dict['node_id'],
                "gh_issue_id": issue_dict['id'],
                "gh_issue_number": issue_dict['number'],
                "gh_user_id": issue_dict['user']['id'],
                "tool_source": self.tool_source,
                "tool_version": self.tool_version,
                "data_source": self.data_source
            }

            # Commit insertion to the issues table
            self.db.execute(self.issues_table.insert().values(issue))
            print("Inserted issue with our issue_id being: ", self.issue_id_inc, 
                "and title of: ", issue_dict['title'], "and gh_issue_num of: ", issue_dict['number'], "\n")

            # Just to help me figure out cases where a..nee vs a..nees shows up
            if "assignee" in issue_dict and "assignees" in issue_dict:
                print("assignee and assignees here\n")
            elif "assignees" in issue_dict:
                print("multiple assignees and no single one\n")
            elif "assignee" in issue_dict:
                print("single assignees and no multiples\n")

            # Check if the assignee key's value is already recorded in the assignees key's value
            #   Create a collective list of unique assignees
            collected_assignees = issue_dict['assignees']
            if issue_dict['assignee'] not in collected_assignees:
                collected_assignees.append(issue_dict['assignee'])
            print("Count of assignees for this issue: ", len(collected_assignees), "\n")

            # Handles case if there are no assignees
            if collected_assignees[0] is not None:
                for assignee_dict in collected_assignees:

                    assignee = {
                        "issue_id": self.issue_id_inc,
                        "cntrb_id": self.find_id_from_login(assignee_dict['login']),
                        "tool_source": self.tool_source,
                        "tool_version": self.tool_version,
                        "data_source": self.data_source
                    }
                    # Commit insertion to the assignee table
                    self.db.execute(self.issue_assignees_table.insert().values(assignee))
                    print("Inserted assignee for issue id: ", self.issue_id_inc, 
                        "with login/cntrb_id: ", assignee_dict['login'], assignee['cntrb_id'], "\n")


            

            # Insert the issue labels to the issue_labels table
            for label_dict in issue_dict['labels']:
                print(label_dict)
                desc = None
                if label_dict['description'] is not None:
                    desc = label_dict['description']
                label = {
                    "issue_id": self.issue_id_inc,
                    "label_text": label_dict["name"],
                    "label_description": desc,
                    "label_color": label_dict['color'],
                    "tool_source": self.tool_source,
                    "tool_version": self.tool_version,
                    "data_source": self.data_source
                }

                self.db.execute(self.issue_labels_table.insert().values(label))
                print("Inserted issue label with text: ", label_dict['name'], "\n")


            #### Messages/comments and events insertion (we collected events above but never inserted them)

            comments_url = (url + "/comments")
            print("Hitting endpoint: ", comments_url, " ...\n")
            r = requests.get(url=comments_url)
            self.update_rate_limit()
            issue_comments = r.json()

            # Add the FK of our cntrb_id to each comment dict to be inserted
            for comment in issue_comments:
                comment['cntrb_id'] = self.find_id_from_login(comment['user']['login'])

            # Filter duplicates before insertion
            comments_need_insertion = self.filter_duplicates({'msg_timestamp': 'created_at', 'cntrb_id': 'cntrb_id'}, ['message'], issue_comments)
    
            print("Number of comments needing insertion: ", len(comments_need_insertion))

            

            for comment in comments_need_insertion:
                issue_comment = {
                    "pltfrm_id": 25150,
                    "msg_text": comment['body'],
                    "msg_timestamp": comment['created_at'],
                    "cntrb_id": self.find_id_from_login(comment['user']['login']),
                    # "cntrb_id": self.find_id_from_login(comment['user']['login']),
                    "tool_source": self.tool_source,
                    "tool_version": self.tool_version,
                    "data_source": self.data_source
                }

                self.db.execute(self.message_table.insert().values(issue_comment))
                print("Inserted issue comment: ", comment['body'], "\n")

                ### ISSUE MESSAGE REF TABLE ###

                issue_message_ref = {
                    "issue_id": self.issue_id_inc,
                    "msg_id": self.msg_id_inc,
                    "tool_source": self.tool_source,
                    "tool_version": self.tool_version,
                    "data_source": self.data_source
                }

                self.db.execute(self.issues_message_ref_table.insert().values(issue_message_ref))

                self.msg_id_inc += 1


            for event in issue_events:
                event['cntrb_id'] = self.find_id_from_login(event['actor']['login'])
            events_need_insertion = self.filter_duplicates({'node_id': 'node_id'}, ['issue_events'], issue_events)
        
            print("Number of events needing insertion: ", len(events_need_insertion))

            for event in issue_events:
                issue_event = {
                    "issue_id": self.issue_id_inc,
                    "node_id": event['node_id'],
                    "node_url": event['url'],
                    "cntrb_id": self.find_id_from_login(event['actor']['login']), #need to insert this cntrb and check for dupe
                    "action": event["event"],
                    "action_commit_hash": event["commit_id"],
                    "tool_source": self.tool_source,
                    "tool_version": self.tool_version,
                    "data_source": self.data_source
                }

                self.db.execute(self.issue_events_table.insert().values(issue_event))
                print("Inserted issue event: ", event['event'], " ", self.issue_id_inc,"\n")
            


            self.issue_id_inc += 1

            task_completed = entry_info.to_dict()
            task_completed['worker_id'] = self.config['id']
            print("Telling broker we completed task: ", task_completed)

            requests.post('http://localhost:5000/api/completed_task', json=task_completed)

            print("\n\n")
            
    def filter_duplicates(self, cols, tables, og_data):
        need_insertion = []

        table_str = tables[0]
        del tables[0]
        for table in tables:
            table_str += ", " + table
        print(cols, tables)
        for col in cols.keys():
            colSQL = s.sql.text("""
                SELECT {} FROM {}
                """.format(col, table_str))

            values = pd.read_sql(colSQL, self.db, params={})
            # logins = rs.json()
            for obj in og_data:
                try:
                    if values.isin([obj[cols[col]]]).any().any():
                        print("value of tuple exists: ", obj[cols[col]], "\n")
                    else:
                        need_insertion.append(obj)
                except:
                    print("RATE LIMIT EXCEEDED, last response: ", og_data)
        return need_insertion

    def find_id_from_login(self, login):
        idSQL = s.sql.text("""
            SELECT cntrb_id FROM contributors WHERE cntrb_login = '{}'
            """.format(login))
        rs = pd.read_sql(idSQL, self.db, params={})
        data_list = [list(row) for row in rs.itertuples(index=False)] 
        print(data_list, login)
        try:
            return data_list[0][0]
        except:
            print("contributor needs to be added...")
            cntrb_url = ("https://api.github.com/users/" + login)

            r = requests.get(url=cntrb_url)
            self.update_rate_limit()
            contributor = r.json()

            # "company": contributor['company'],
            # "location": contributor['location'],
            # "email": contributor['email'],

            # aliasSQL = s.sql.text("""
            #     SELECT canonical_email
            #     FROM contributors_aliases
            #     WHERE alias_email = {}
            # """.format(contributor['email']))
            # rs = pd.read_sql(aliasSQL, self.db, params={})

            canonical_email = None#rs.iloc[0]["canonical_email"]

            cntrb = {
                "cntrb_login": contributor['login'],
                "cntrb_created_at": contributor['created_at'],
                # "cntrb_type": , ?asking sean
                "cntrb_canonical": canonical_email,
                "gh_user_id": contributor['id'],
                "gh_login": contributor['login'],
                "gh_url": contributor['url'],
                "gh_html_url": contributor['html_url'],
                "gh_node_id": contributor['node_id'],
                "gh_avatar_url": contributor['avatar_url'],
                "gh_gravatar_id": contributor['gravatar_id'],
                "gh_followers_url": contributor['followers_url'],
                "gh_following_url": contributor['following_url'],
                "gh_gists_url": contributor['gists_url'],
                "gh_starred_url": contributor['starred_url'],
                "gh_subscriptions_url": contributor['subscriptions_url'],
                "gh_organizations_url": contributor['organizations_url'],
                "gh_repos_url": contributor['repos_url'],
                "gh_events_url": contributor['events_url'],
                "gh_received_events_url": contributor['received_events_url'],
                "gh_type": contributor['type'],
                "gh_site_admin": contributor['site_admin'],
                "tool_source": self.tool_source,
                "tool_version": self.tool_version,
                "data_source": self.data_source
            }
            self.db.execute(self.contributors_table.insert().values(cntrb))
            print("Inserted contributor: ", contributor['login'], "\n")
            self.cntrb_id_inc += 1
            self.find_id_from_login(login)
            pass

    def update_rate_limit(self):
        self.rate_limit -= 1
        if self.rate_limit <= 0:

            url = "https://api.github.com/users/gabe-heim"
            response = requests.get(url=url)
            reset_time = response.headers['X-RateLimit-Reset']
        
            time_diff = datetime.datetime.fromtimestamp(reset_time) - datetime.datetime.now()
            print("Rate limit exceeded, waiting ", time_diff.total_seconds(), " seconds.\n")
            time.sleep(time_diff.total_seconds())
            self.rate_limit = int(response.headers['X-RateLimit-Limit'])
        


            

        